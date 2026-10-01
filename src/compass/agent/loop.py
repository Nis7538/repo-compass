"""A tool-use loop with hard limits: turns, tokens per response, and dollars.

The loop is the same for every tool condition (none, baseline, compass, both): only
the ToolSet changes. M5 relies on that, so nothing here may depend on which tools are
present beyond "are there any".

It imports nothing from the anthropic SDK. `client` is anything with
`client.messages.create(**kwargs)` returning an object shaped like the SDK's Message
(`content` blocks with a `type`, `stop_reason`, `usage`). The CLI passes a real
`anthropic.Anthropic()`; tests pass a scripted fake.

The cost cap is enforced before each request, never after (docs/adr/008):

1. The request's prompt is bounded from above: the previous request's actual prompt
   tokens, plus its output tokens (the assistant turn that is now part of the
   prompt), plus an estimate of what was appended since (tool results, the wrap-up
   note). The estimate is chars / 3 with 10% and a per-block allowance on top;
   chars / 3 alone already overestimates code (ADR-005). The first request is
   estimated from its own characters plus an allowance for the API's tool-use
   system prompt.
2. That prompt is priced at the cache-write rate, the dearest input rate.
3. max_tokens for the request is cut to what the rest of the budget can pay for at
   the output rate. Output, thinking included, cannot exceed max_tokens.
4. If that leaves fewer than MIN_OUTPUT_TOKENS, the request is not sent.
   (Exploring turns need MIN_EXPLORE_TOKENS; below that the loop moves to the
   wrap-up described next.)

So actual spend stays at or under the cap as long as the prompt bound holds.

Exploring turns also keep back a reserve for one last answer. When the next exploring
turn would eat into it, or one request is left under max_turns, the loop sends a
wrap-up request instead: same tools, tool_choice none, and a note asking for the
answer now. A run cut short by its limits still ends with an answer when the budget
allows one.
"""

import json
import math
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from compass.agent.pricing import Usage, cost, price_of
from compass.tools.render import clip

MIN_OUTPUT_TOKENS = 1024
# An exploring turn needs room to think and call tools; one cut by max_tokens ends the
# run without an answer, so exploration stops (and the wrap-up starts) below this.
MIN_EXPLORE_TOKENS = 4096
# Output kept back during exploration so the final answer can still be written.
WRAP_UP_OUTPUT_TOKENS = 4096
# Tool results one exploring turn may add (two full answers at the largest cap).
TOOL_RESULT_ALLOWANCE = 4000
# The API's own tool-use system prompt (286 tokens on current models, up to 675 on
# older ones, per the pricing page) and message framing.
TOOL_PROMPT_ALLOWANCE = 1000
BLOCK_ALLOWANCE = 50
ESTIMATE_MARGIN = 1.1

WRAP_UP_NOTE = (
    "Your tool budget for this task is used up. Do not call tools. Write your final "
    "answer now from what you have found, and say what you could not check."
)

# Why a run ended.
END_TURN = "end_turn"  # the model finished on its own
MAX_TURNS = "max_turns"
COST_CAP = "cost_cap"
MAX_TOKENS = "max_tokens"  # a response hit max_tokens
REFUSAL = "refusal"
ERROR = "error"  # the API call raised


class ToolSet(Protocol):
    def definitions(self) -> list[dict]:
        """Tool definitions for the API: name, description, input_schema."""
        ...

    def call(self, name: str, arguments: dict) -> tuple[str, bool]:
        """Run one tool. Returns (text, is_error); never raises."""
        ...


@dataclass(frozen=True)
class Limits:
    max_turns: int = 20  # API requests, the wrap-up included
    max_tokens: int = 16000  # per response
    max_cost_usd: float = 1.00


@dataclass
class RunResult:
    text: str
    stop_reason: str
    final_api_stop_reason: str | None
    wrap_up: bool
    turns: int
    tool_calls: Counter = field(default_factory=Counter)
    tool_errors: int = 0
    usage: Usage = field(default_factory=Usage)
    cost_usd: float = 0.0
    wall_s: float = 0.0
    error: str | None = None
    messages: list = field(default_factory=list)


def estimate(chars: int) -> int:
    return math.ceil(chars / 3 * ESTIMATE_MARGIN)


def run_agent(
    client: Any,
    model: str,
    system: str,
    user: str,
    toolset: ToolSet,
    limits: Limits,
    clock: Callable[[], float] = time.monotonic,
) -> RunResult:
    price = price_of(model)
    tools = toolset.definitions()
    messages: list[dict] = [{"role": "user", "content": user}]
    request = {"model": model, "system": system, "cache_control": {"type": "ephemeral"}}
    if tools:
        request["tools"] = tools

    result = RunResult("", END_TURN, None, False, 0, messages=messages)
    start = clock()
    # Upper bound on the next request's prompt tokens.
    prompt_bound = estimate(len(system) + len(json.dumps(tools)) + len(user))
    if tools:
        prompt_bound += TOOL_PROMPT_ALLOWANCE
    wrapping = False  # the next request is the wrap-up
    ended_by: str | None = None  # the limit that ended exploration

    while True:
        last = wrapping or result.turns + 1 >= limits.max_turns
        max_tokens = _affordable(limits, price, result.cost_usd, prompt_bound, reserve=not last)
        if max_tokens < MIN_EXPLORE_TOKENS and not last:
            # No room to explore further: spend what is left on the answer.
            ended_by, last = COST_CAP, True
            max_tokens = _affordable(limits, price, result.cost_usd, prompt_bound, False)
            _note_wrap_up(messages)
        if max_tokens < MIN_OUTPUT_TOKENS:
            result.stop_reason = COST_CAP
            break

        kwargs = {**request, "messages": messages, "max_tokens": max_tokens}
        if last and tools:
            kwargs["tool_choice"] = {"type": "none"}
        try:
            response = client.messages.create(**kwargs)
        except Exception as exc:  # any API failure ends the run; the log records it
            result.stop_reason = ERROR
            result.error = clip(f"{type(exc).__name__}: {exc}", 500)
            break
        result.turns += 1
        result.wrap_up = result.wrap_up or (last and ended_by is not None)
        usage = _usage(response)
        result.usage.add(usage)
        result.cost_usd += cost(usage, price)
        result.final_api_stop_reason = response.stop_reason
        messages.append({"role": "assistant", "content": response.content})
        result.text = _text(response.content)

        tool_uses = [b for b in response.content if b.type == "tool_use"]
        if response.stop_reason != "tool_use" or not tool_uses or last:
            result.stop_reason = ended_by or _ended(response.stop_reason)
            break

        results, chars = _run_tools(toolset, tool_uses, result)
        messages.append({"role": "user", "content": results})
        prompt_bound = (
            usage.prompt_tokens
            + usage.output_tokens
            + estimate(chars)
            + BLOCK_ALLOWANCE * (len(results) + 1)
        )
        if result.turns + 1 >= limits.max_turns:
            ended_by = MAX_TURNS
        elif _affordable(limits, price, result.cost_usd, prompt_bound, True) < MIN_EXPLORE_TOKENS:
            ended_by = COST_CAP
        if ended_by:
            wrapping = True
            _note_wrap_up(messages)
            prompt_bound += estimate(len(WRAP_UP_NOTE))

    result.wall_s = clock() - start
    return result


def _affordable(limits: Limits, price, spent: float, prompt: int, reserve: bool) -> int:
    """The largest max_tokens the remaining budget can pay for, capped at limits.max_tokens.

    With reserve, room is also kept for a wrap-up request after this one: its prompt
    is this one's plus this one's output plus tool results, and its output is
    WRAP_UP_OUTPUT_TOKENS. The output of this request (m) appears twice, once as
    output and once as part of the next prompt, hence the (output + cache_write) rate.
    """
    per_token = price.cache_write / 1_000_000
    per_out = price.output / 1_000_000
    left = limits.max_cost_usd - spent - prompt * per_token
    rate = per_out
    if reserve:
        left -= (prompt + TOOL_RESULT_ALLOWANCE) * per_token + WRAP_UP_OUTPUT_TOKENS * per_out
        rate = per_out + per_token
    if left <= 0:
        return 0
    return min(limits.max_tokens, math.floor(left / rate))


def _note_wrap_up(messages: list[dict]) -> None:
    """Ask for the answer, in the user message that will be sent next.

    The note joins the tool results (text after tool_result blocks), so the
    conversation keeps alternating user/assistant and nothing already sent changes.
    """
    last = messages[-1]
    if last["role"] == "user" and isinstance(last["content"], list):
        last["content"].append({"type": "text", "text": WRAP_UP_NOTE})


def _run_tools(toolset: ToolSet, tool_uses: list, result: RunResult) -> tuple[list[dict], int]:
    """Every tool call of one turn, answered in one user message, in call order."""
    blocks = []
    chars = 0
    for use in tool_uses:
        result.tool_calls[use.name] += 1
        arguments = use.input if isinstance(use.input, dict) else {}
        try:
            text, is_error = toolset.call(use.name, arguments)
        except Exception as exc:  # a ToolSet should not raise; if it does, the agent hears
            text, is_error = clip(f"Tool failed: {type(exc).__name__}: {exc}", 500), True
        if is_error:
            result.tool_errors += 1
        block = {"type": "tool_result", "tool_use_id": use.id, "content": text}
        if is_error:
            block["is_error"] = True
        blocks.append(block)
        chars += len(text)
    return blocks, chars


def _usage(response) -> Usage:
    u = response.usage
    return Usage(
        input_tokens=u.input_tokens or 0,
        cache_creation_input_tokens=getattr(u, "cache_creation_input_tokens", 0) or 0,
        cache_read_input_tokens=getattr(u, "cache_read_input_tokens", 0) or 0,
        output_tokens=u.output_tokens or 0,
    )


def _text(content: list) -> str:
    return "\n\n".join(b.text for b in content if b.type == "text" and b.text.strip()).strip()


def _ended(api_stop_reason: str | None) -> str:
    if api_stop_reason in (None, "end_turn", "stop_sequence", "tool_use"):
        return END_TURN
    return api_stop_reason  # max_tokens, refusal, pause_turn, ...


def to_jsonable(messages: list) -> list:
    """The conversation as plain JSON (SDK blocks via model_dump), for a transcript file."""

    def block(b):
        if isinstance(b, dict):
            return b
        if hasattr(b, "model_dump"):
            return b.model_dump(mode="json", exclude_none=True)
        return {k: v for k, v in vars(b).items() if v is not None}

    out = []
    for m in messages:
        content = m["content"]
        if isinstance(content, list):
            content = [block(b) for b in content]
        out.append({"role": m["role"], "content": content})
    return out
