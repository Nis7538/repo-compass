"""A scripted stand-in for the anthropic client, so the agent loop runs without network.

Responses mirror the SDK's shape (Message.content blocks with a `type`, stop_reason,
usage) closely enough for compass.agent.loop, which never imports the SDK. Every
request is recorded as a deep copy, because the loop keeps appending to the same
messages list after a request is sent.
"""

import copy
import json
import math
from collections.abc import Callable
from dataclasses import dataclass, field


@dataclass
class Text:
    text: str
    type: str = "text"


@dataclass
class Thinking:
    thinking: str = ""
    signature: str = "sig"
    type: str = "thinking"


@dataclass
class ToolUse:
    id: str
    name: str
    input: dict
    type: str = "tool_use"


@dataclass
class FakeUsage:
    input_tokens: int = 100
    output_tokens: int = 50
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0


@dataclass
class Message:
    content: list
    stop_reason: str
    usage: FakeUsage = field(default_factory=FakeUsage)


def reply(*blocks, stop: str | None = None, usage: FakeUsage | None = None) -> Message:
    """A response; stop defaults to tool_use when a block calls a tool, else end_turn."""
    if stop is None:
        stop = "tool_use" if any(b.type == "tool_use" for b in blocks) else "end_turn"
    return Message(list(blocks), stop, usage or FakeUsage())


class FakeClient:
    """client.messages.create(**kwargs) answers from a script.

    A script item is a Message, an Exception to raise, or a function of the request
    kwargs returning a Message (for scripts that depend on the request).
    """

    def __init__(self, script: list | Callable[[dict], Message]):
        self.script = script
        self.requests: list[dict] = []
        self.messages = self
        self.counted: list[dict] = []

    def create(self, **kwargs) -> Message:
        self.requests.append(copy.deepcopy(kwargs))
        item = self.script(kwargs) if callable(self.script) else self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item(kwargs) if callable(item) else item

    def count_tokens(self, **kwargs):
        """Tokens as chars / 4 of the request, so tests can predict the answer."""
        self.counted.append(kwargs)

        @dataclass
        class Count:
            input_tokens: int

        return Count(request_chars(kwargs) // 4)


def request_chars(kwargs: dict) -> int:
    """Characters of everything a request sends to the model."""

    def plain(value):
        if isinstance(value, list):
            return [plain(v) for v in value]
        if isinstance(value, dict):
            return {k: plain(v) for k, v in value.items()}
        if hasattr(value, "__dataclass_fields__"):
            return {k: plain(v) for k, v in vars(value).items()}
        return value

    parts = [kwargs.get("system", ""), kwargs.get("tools", []), kwargs["messages"]]
    return len(json.dumps(plain(parts)))


def worst_case_client(tool_name: str = "echo") -> FakeClient:
    """Every response costs as much as a request allows.

    The prompt is reported as ceil(chars / 3) of the whole request, all of it written
    to cache (the dearest input rate), and the output uses all of max_tokens. It always
    calls a tool unless tool_choice is none.
    """
    counter = iter(range(1, 10_000))

    def respond(kwargs: dict) -> Message:
        usage = FakeUsage(
            input_tokens=0,
            cache_creation_input_tokens=math.ceil(request_chars(kwargs) / 3),
            output_tokens=kwargs["max_tokens"],
        )
        if kwargs.get("tool_choice") == {"type": "none"} or "tools" not in kwargs:
            return reply(Text("final answer"), usage=usage)
        n = next(counter)
        return reply(ToolUse(f"t{n}", tool_name, {"n": n}), usage=usage)

    return FakeClient(respond)
