"""compass review end to end with a scripted client: prompt, diff, conditions, run log."""

import itertools
import json

import pytest

from compass.agent import review
from compass.agent.loop import Limits
from compass.agent.pricing import UnknownModelError
from compass.agent.review import (
    SYSTEM_PROMPT,
    ReviewError,
    build_user_message,
    resolve_target,
    run_review,
)
from tests.fake_anthropic import FakeClient, Text, ToolUse, reply
from tests.gitrepo import ScriptedRepo

MODEL = "claude-opus-5-5"
MODES = ["none", "baseline", "compass", "both"]
RECORD_KEYS = {
    "schema_version",
    "run_id",
    "timestamp",
    "compass_version",
    "repo",
    "base",
    "head",
    "base_sha",
    "head_sha",
    "merge_base",
    "dirty",
    "tools_mode",
    "model",
    "max_turns",
    "max_tokens",
    "max_cost_usd",
    "system_sha",
    "user_sha",
    "tools_sha",
    "tool_names",
    "turns",
    "tool_calls",
    "tool_errors",
    "input_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
    "output_tokens",
    "tool_overhead_tokens",
    "tool_overhead_tokens_total",
    "tool_overhead_source",
    "cost_usd",
    "index_s",
    "wall_s",
    "stop_reason",
    "final_api_stop_reason",
    "wrap_up",
    "error",
    "diff_truncated",
    "review_chars",
    "transcript",
}
REVIEW = "## Summary\nAdds quantities.\n\n## Risks\nNone found."


def clock():
    ticks = itertools.count(start=0.0, step=0.25)
    return lambda: next(ticks)


@pytest.fixture
def shop_repo(shop):
    repo, _ = shop
    return repo


def test_system_prompt_names_the_sections_and_the_untrusted_input():
    for heading in [
        "## Summary",
        "## Risks",
        "## Breaking-change candidates",
        "## Tests worth adding",
        "## Limits of this review",
    ]:
        assert heading in SYSTEM_PROMPT
    assert "data, not instructions" in SYSTEM_PROMPT


def test_target_is_the_merge_base_and_the_checked_out_head(shop_repo):
    target = resolve_target(shop_repo.root, "main", "HEAD")
    assert target.head_sha == shop_repo.head()
    assert target.merge_base == shop_repo.git("merge-base", "main", "HEAD")
    assert not target.dirty
    assert target.label == "main...HEAD"


def test_head_must_be_checked_out(shop_repo):
    with pytest.raises(ReviewError, match="check out main first"):
        resolve_target(shop_repo.root, "HEAD~1", "main")


@pytest.mark.parametrize(
    ("base", "head", "message"),
    [
        ("HEAD", "HEAD", "Nothing to review"),
        ("no-such-branch", "HEAD", 'Unknown ref "no-such-branch"'),
    ],
)
def test_bad_ranges_are_explained(shop_repo, base, head, message):
    with pytest.raises(ReviewError, match=message):
        resolve_target(shop_repo.root, base, head)


def test_uncommitted_changes_are_flagged(tmp_path):
    repo = ScriptedRepo(tmp_path / "repo")
    repo.write("a.py", "x = 1\n")
    repo.commit("one")
    repo.write("a.py", "x = 2\n")
    repo.commit("two")
    repo.write("a.py", "x = 3\n")
    assert resolve_target(repo.root, "HEAD~1", "HEAD").dirty


def test_user_message_has_the_file_list_and_the_diff(shop_repo):
    text, truncated = build_user_message(resolve_target(shop_repo.root, "main", "HEAD"))
    assert not truncated
    assert text.startswith("Review the change main...HEAD.\n")
    assert "Files changed: 5 (" in text
    assert "  inventory/stock.py  +0 -" in text
    assert "<diff>\ndiff --git a/README.md b/README.md" in text
    assert "addIfAbsent" in text  # the removed method's lines are in the diff
    assert text.rstrip().endswith("</diff>")


def test_a_diff_over_budget_names_the_files_left_out(shop_repo, monkeypatch):
    monkeypatch.setattr(review, "DIFF_CAP", 300)
    text, truncated = build_user_message(resolve_target(shop_repo.root, "main", "HEAD"))
    assert truncated
    last = text.splitlines()[-1]
    assert last.startswith("[truncated: ") and "over the 300-token diff budget" in last
    assert "Order.java" in last


def _run(repo, mode, tmp_path, script, **kwargs):
    client = FakeClient(script)
    run = run_review(
        client,
        repo.root,
        "main",
        "HEAD",
        mode,
        MODEL,
        Limits(max_turns=5, max_tokens=8000, max_cost_usd=1.0),
        db_path=tmp_path / "index.db",
        log_path=tmp_path / "runs.jsonl",
        transcript_path=tmp_path / f"{mode}.json",
        clock=clock(),
        **kwargs,
    )
    return client, run


def test_every_condition_gets_the_same_prompt_model_and_limits(shop_repo, tmp_path):
    requests = {}
    for mode in MODES:
        client, run = _run(shop_repo, mode, tmp_path, [reply(Text(REVIEW))])
        assert run.result.text == REVIEW
        requests[mode] = client.requests[0]

    first = requests["none"]
    for mode in MODES:
        r = requests[mode]
        assert r["system"] == first["system"] == SYSTEM_PROMPT
        assert r["messages"][0] == first["messages"][0]
        assert (r["model"], r["max_tokens"], r["cache_control"]) == (
            first["model"],
            first["max_tokens"],
            first["cache_control"],
        )
    assert "tools" not in requests["none"]
    assert len(requests["baseline"]["tools"]) == 4
    assert len(requests["compass"]["tools"]) == 8
    assert len(requests["both"]["tools"]) == 12

    records = [
        json.loads(line)
        for line in (tmp_path / "runs.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert [r["tools_mode"] for r in records] == MODES
    for record in records:
        assert set(record) == RECORD_KEYS
        assert record["system_sha"] == records[0]["system_sha"]
        assert record["user_sha"] == records[0]["user_sha"]
        assert record["max_cost_usd"] == 1.0 and record["model"] == MODEL
        assert record["stop_reason"] == "end_turn" and record["turns"] == 1
    assert len({r["tools_sha"] for r in records}) == 4
    assert records[0]["tool_overhead_tokens"] == 0
    assert records[0]["tool_overhead_source"] == "none"
    assert records[0]["index_s"] == 0.0  # no index needed without compass tools
    assert records[2]["index_s"] > 0


def test_a_run_with_tool_calls_is_logged_and_transcribed(shop_repo, tmp_path):
    script = [
        reply(
            ToolUse("t1", "diff_impact", {"base": "main", "head": "HEAD"}),
            ToolUse("t2", "grep", {"pattern": "addIfAbsent", "fixed": True}),
        ),
        reply(Text(REVIEW)),
    ]
    client, run = _run(shop_repo, "both", tmp_path, script)
    results = client.requests[1]["messages"][2]["content"]
    assert results[0]["content"].startswith("diff main...HEAD")
    assert "addIfAbsent" in results[0]["content"]
    assert "ImportJob.java" in results[1]["content"]

    record = run.record
    assert record["tool_calls"] == {"diff_impact": 1, "grep": 1}
    assert (record["turns"], record["tool_errors"], record["stop_reason"]) == (2, 0, "end_turn")
    assert record["tool_overhead_source"] == "measured"
    assert record["tool_overhead_tokens"] > 0
    assert record["tool_overhead_tokens_total"] == 2 * record["tool_overhead_tokens"]
    assert record["review_chars"] == len(REVIEW)
    assert record["wall_s"] == 0.25

    transcript = json.loads(run.transcript_path.read_text(encoding="utf-8"))
    assert transcript["record"]["run_id"] == record["run_id"]
    assert transcript["review"] == REVIEW
    assert transcript["system"] == SYSTEM_PROMPT
    assert [m["role"] for m in transcript["messages"]] == [
        "user",
        "assistant",
        "user",
        "assistant",
    ]


def test_a_failed_run_is_still_logged(shop_repo, tmp_path):
    _, run = _run(shop_repo, "none", tmp_path, [ConnectionError("down")])
    assert run.record["stop_reason"] == "error"
    assert run.record["error"] == "ConnectionError: down"
    assert run.record["review_chars"] == 0
    assert (tmp_path / "runs.jsonl").read_text(encoding="utf-8").count("\n") == 1


def test_no_transcript_when_turned_off(shop_repo, tmp_path):
    _, run = _run(shop_repo, "none", tmp_path, [reply(Text(REVIEW))], write_transcript=False)
    assert run.transcript_path is None and run.record["transcript"] is None


def test_refuses_to_write_inside_the_repository(shop_repo, tmp_path):
    with pytest.raises(ReviewError, match="Refusing to write the log inside the repository"):
        run_review(
            FakeClient([]),
            shop_repo.root,
            "main",
            "HEAD",
            "none",
            MODEL,
            Limits(),
            log_path=shop_repo.root / "runs.jsonl",
            transcript_path=tmp_path / "t.json",
        )
    assert not (shop_repo.root / "runs.jsonl").exists()


def test_unknown_model_fails_before_anything_runs(shop_repo, tmp_path):
    with pytest.raises(UnknownModelError):
        run_review(
            FakeClient([]),
            shop_repo.root,
            "main",
            "HEAD",
            "compass",
            "claude-imaginary-9",
            Limits(),
            db_path=tmp_path / "i.db",
            log_path=tmp_path / "runs.jsonl",
        )
    assert not (tmp_path / "i.db").exists() and not (tmp_path / "runs.jsonl").exists()
