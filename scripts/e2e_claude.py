"""Ask Claude Code one question about a repository with the compass tools, headless.

MANUAL ONLY. It runs `claude -p`, which calls the model and costs money, so it never
runs in CI: pytest does not collect scripts/, and the script refuses to start when
the CI environment variable is set.

    uv run python scripts/e2e_claude.py --repo PATH "question" [--model M] [--builtin]

The compass server is started from this checkout's Python with `--repo .`, from
inside the target repository, the same way the README registers it. By default
Claude Code's built-in tools are removed (`--tools ""`), so every answer is a test
of the compass tools alone; --builtin keeps Read/Grep/Glob as well. The target
repository is never modified by this script.

Prints each tool call with its arguments and the answer's size (estimated tokens,
the same ceil(chars / 3) the caps use), then turns, cost and the final answer.
--log keeps the raw stream-json transcript.
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from compass.tools.render import estimate_tokens  # noqa: E402

DEFAULT_MODEL = "claude-sonnet-5-5"  # the M2 run used it; keep runs comparable


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("question")
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--builtin", action="store_true", help="also allow Read, Grep, Glob")
    parser.add_argument("--log", type=Path, help="write the raw stream-json transcript here")
    args = parser.parse_args()
    if os.environ.get("CI"):
        sys.exit("e2e_claude.py runs Claude Code against the API and is manual only; not in CI.")

    server = {"command": sys.executable, "args": ["-m", "compass.cli", "serve", "--repo", "."]}
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as cfg:
        json.dump({"mcpServers": {"repo-compass": server}}, cfg)
    command = [
        "claude",
        "-p",
        args.question,
        "--output-format",
        "stream-json",
        "--verbose",
        "--model",
        args.model,
        "--mcp-config",
        cfg.name,
        "--strict-mcp-config",
        "--allowedTools",
        "mcp__repo-compass",
    ]
    command += ["--tools", "Read,Grep,Glob"] if args.builtin else ["--tools", ""]
    try:
        proc = subprocess.run(
            command, cwd=args.repo, capture_output=True, text=True, encoding="utf-8"
        )
    finally:
        os.unlink(cfg.name)
    if args.log:
        args.log.write_text(proc.stdout, encoding="utf-8")
    _report(proc.stdout.splitlines())
    if proc.returncode != 0:
        sys.exit(f"claude exited {proc.returncode}: {proc.stderr.strip()[:500]}")


def _report(lines: list[str]) -> None:
    calls: dict[str, str] = {}
    for line in lines:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        content = event.get("message", {}).get("content", [])
        if event.get("type") == "assistant":
            for block in content:
                if block.get("type") == "tool_use":
                    name = block["name"].removeprefix("mcp__repo-compass__")
                    calls[block["id"]] = f"{name} {json.dumps(block['input'])}"
        elif event.get("type") == "user" and isinstance(content, list):
            for block in content:
                if block.get("type") == "tool_result":
                    text = _text(block.get("content"))
                    call = calls.get(block["tool_use_id"], "?")
                    print(f"- {call} -> {estimate_tokens(text)} tokens")
        elif event.get("type") == "result":
            cost = event.get("total_cost_usd", 0.0)
            print(f"\nturns {event.get('num_turns')}, cost ${cost:.3f}\n")
            print(event.get("result", ""))


def _text(content) -> str:
    if isinstance(content, str):
        return content
    return "".join(part.get("text", "") for part in content or [])


if __name__ == "__main__":
    main()
