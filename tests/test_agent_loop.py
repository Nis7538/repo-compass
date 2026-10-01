"""The agent loop against a scripted client: no network, no anthropic SDK, exact results."""

import itertools

import pytest

from compass.agent import loop
from compass.agent.loop import Limits, run_agent
from compass.agent.pricing import UnknownModelError, Usage, cost, price_of
from tests.fake_anthropic import (
    FakeClient,
    FakeUsage,
    Text,
    Thinking,
    ToolUse,
    reply,
    worst_case_client,
)

MODEL = "claude-opus-5-5"
SYSTEM = "You review code."
USER = "Review this diff."


class EchoTools:
    """echo answers with its arguments; boom raises; anything else is unknown."""

    def __init__(self, tools=("echo", "boom")):
        self.names = tools

    def definitions(self):
        schema = {"type": "object", "properties": {}}
        return [{"name": n, "description": n, "input_schema": schema} for n in self.names]

    def call(self, name, arguments):
        if name == "echo":
            return f"echo {arguments}", False
        if name == "boom":
            raise RuntimeError("tool exploded")
        return f"Unknown tool {name}", True


class NoTools:
    def definitions(self):
        return []

    def call(self, name, arguments):
        return f"Unknown tool {name}", True


def clock():
    ticks = itertools.count(start=100.0, step=1.5)
    return lambda: next(ticks)


def test_tool_round_trip():
    first = reply(
        Thinking(),
        Text("Checking callers."),
        ToolUse("t1", "echo", {"x": 1}),
        ToolUse("t2", "echo", {"x": 2}),
        usage=FakeUsage(100, 40, 10, 5),
    )
    client = FakeClient([first, reply(Text("## Summary\nFine."), usage=FakeUsage(300, 60))])
    result = run_agent(client, MODEL, SYSTEM, USER, EchoTools(), Limits(), clock())

    assert result.text == "## Summary\nFine."
    assert (result.stop_reason, result.final_api_stop_reason) == ("end_turn", "end_turn")
    assert result.turns == 2 and not result.wrap_up
    assert result.tool_calls == {"echo": 2} and result.tool_errors == 0
    assert result.usage == Usage(400, 10, 5, 100)
    assert result.cost_usd == pytest.approx(cost(Usage(400, 10, 5, 100), price_of(MODEL)))
    assert result.wall_s == 1.5

    second = client.requests[1]
    assert second["system"] == SYSTEM and second["model"] == MODEL
    assert second["cache_control"] == {"type": "ephemeral"}
    assert "tool_choice" not in second
    # The assistant turn goes back unchanged (thinking block included), then every
    # result of that turn in one user message, in call order.
    assert second["messages"][1] == {"role": "assistant", "content": first.content}
    assert second["messages"][2] == {
        "role": "user",
        "content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": "echo {'x': 1}"},
            {"type": "tool_result", "tool_use_id": "t2", "content": "echo {'x': 2}"},
        ],
    }


def test_tool_errors_are_reported_to_the_agent_and_the_loop_continues():
    client = FakeClient(
        [
            reply(ToolUse("a", "boom", {}), ToolUse("b", "nope", {}), ToolUse("c", "echo", {})),
            reply(Text("done")),
        ]
    )
    result = run_agent(client, MODEL, SYSTEM, USER, EchoTools(), Limits(), clock())
    results = client.requests[1]["messages"][2]["content"]
    assert [r.get("is_error", False) for r in results] == [True, True, False]
    assert results[0]["content"] == "Tool failed: RuntimeError: tool exploded"
    assert results[1]["content"] == "Unknown tool nope"
    assert result.tool_errors == 2
    assert result.tool_calls == {"boom": 1, "nope": 1, "echo": 1}
    assert result.text == "done"


def test_max_turns_ends_with_a_wrap_up_request():
    always = FakeClient(
        lambda kw: (
            reply(Text("final"))
            if kw.get("tool_choice") == {"type": "none"}
            else reply(ToolUse(f"t{len(kw['messages'])}", "echo", {}))
        )
    )
    result = run_agent(always, MODEL, SYSTEM, USER, EchoTools(), Limits(max_turns=3), clock())

    assert result.turns == 3 == len(always.requests)
    assert [r.get("tool_choice") for r in always.requests] == [None, None, {"type": "none"}]
    last_user = always.requests[2]["messages"][-1]["content"]
    assert last_user[-1] == {"type": "text", "text": loop.WRAP_UP_NOTE}
    assert last_user[0]["type"] == "tool_result"  # the note comes after the results
    assert (result.stop_reason, result.wrap_up, result.text) == ("max_turns", True, "final")
    assert result.tool_calls == {"echo": 2}


def test_one_turn_means_no_tools_are_called():
    client = FakeClient([reply(Text("answer"))])
    result = run_agent(client, MODEL, SYSTEM, USER, EchoTools(), Limits(max_turns=1), clock())
    assert client.requests[0]["tool_choice"] == {"type": "none"}
    assert (result.stop_reason, result.wrap_up) == ("end_turn", False)


@pytest.mark.parametrize("cap", [0.05, 0.10, 0.25, 0.50, 1.00, 2.00])
def test_worst_case_spend_never_exceeds_the_cap(cap):
    client = worst_case_client()
    limits = Limits(max_turns=50, max_tokens=16000, max_cost_usd=cap)
    result = run_agent(client, MODEL, SYSTEM, USER, EchoTools(), limits, clock())

    assert result.cost_usd <= cap
    assert result.stop_reason == "cost_cap"
    assert all(r["max_tokens"] >= loop.MIN_OUTPUT_TOKENS for r in client.requests)
    exploring = [r for r in client.requests if "tool_choice" not in r]
    assert all(r["max_tokens"] >= loop.MIN_EXPLORE_TOKENS for r in exploring)
    assert any(r["max_tokens"] < limits.max_tokens for r in client.requests)  # clamped
    # Exploration stops while there is still money for the answer, so the run ends
    # with a wrap-up request rather than mid-exploration.
    if result.turns > 1:
        assert result.wrap_up and result.text == "final answer"
        assert client.requests[-1]["tool_choice"] == {"type": "none"}


def test_too_small_a_budget_sends_nothing():
    client = worst_case_client()
    result = run_agent(
        client, MODEL, SYSTEM, USER, EchoTools(), Limits(max_cost_usd=0.001), clock()
    )
    assert client.requests == []
    assert (result.turns, result.stop_reason, result.text, result.cost_usd) == (
        0,
        "cost_cap",
        "",
        0.0,
    )


def test_budget_for_one_answer_only_skips_exploration():
    # Enough for one request with a short answer, not for exploring and answering.
    client = worst_case_client()
    result = run_agent(
        client, MODEL, SYSTEM, USER, EchoTools(), Limits(max_cost_usd=0.04), clock()
    )
    assert len(client.requests) == 1
    assert client.requests[0]["tool_choice"] == {"type": "none"}
    assert (result.stop_reason, result.wrap_up, result.text) == ("cost_cap", True, "final answer")


@pytest.mark.parametrize("stop", ["max_tokens", "refusal"])
def test_abnormal_api_stop_reasons_end_the_run(stop):
    client = FakeClient([reply(Text("partial"), ToolUse("t1", "echo", {}), stop=stop)])
    result = run_agent(client, MODEL, SYSTEM, USER, EchoTools(), Limits(), clock())
    assert (result.stop_reason, result.final_api_stop_reason) == (stop, stop)
    assert result.turns == 1 and result.tool_calls == {}
    assert result.text == "partial"


def test_api_error_ends_the_run_and_is_recorded():
    client = FakeClient([reply(ToolUse("t1", "echo", {})), ConnectionError("network down")])
    result = run_agent(client, MODEL, SYSTEM, USER, EchoTools(), Limits(), clock())
    assert result.stop_reason == "error"
    assert result.error == "ConnectionError: network down"
    assert result.turns == 1 and result.tool_calls == {"echo": 1}


def test_without_tools_there_is_no_tools_or_tool_choice_parameter():
    client = FakeClient([reply(Text("review"))])
    result = run_agent(client, MODEL, SYSTEM, USER, NoTools(), Limits(max_turns=1), clock())
    assert "tools" not in client.requests[0] and "tool_choice" not in client.requests[0]
    assert result.text == "review"


def test_unknown_model_is_refused_before_any_request():
    client = FakeClient([reply(Text("x"))])
    with pytest.raises(UnknownModelError):
        run_agent(client, "claude-imaginary-9", SYSTEM, USER, NoTools(), Limits(), clock())
    assert client.requests == []


def test_transcript_is_plain_json():
    first = reply(Thinking(), ToolUse("t1", "echo", {"x": 1}))
    client = FakeClient([first, reply(Text("done"))])
    result = run_agent(client, MODEL, SYSTEM, USER, EchoTools(), Limits(), clock())
    transcript = loop.to_jsonable(result.messages)
    assert transcript[0] == {"role": "user", "content": USER}
    assert transcript[1]["content"][1] == {
        "id": "t1",
        "name": "echo",
        "input": {"x": 1},
        "type": "tool_use",
    }
    assert transcript[3]["content"] == [{"text": "done", "type": "text"}]
