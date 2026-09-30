"""Compare the token estimate (ceil(chars / 3)) with real counts from the Claude API.

MANUAL ONLY. It calls the API (network, an API key, a little money), so it never
runs in CI: pytest does not collect scripts/, and the script refuses to start when
the CI environment variable is set.

    uv sync --extra agent
    uv run python scripts/calibrate_tokens.py [--repo PATH] [--model MODEL]

Credentials come from the environment (ANTHROPIC_API_KEY, or an `ant auth login`
profile), never from arguments or files in this repo.

What is measured:
- One answer per tool on the test fixtures and the scripted git history
  (tests/shop_history.py), plus repo_summary, module_dependencies and hotspots on
  --repo if given. Each answer's count is count_tokens(answer) minus
  count_tokens(one character), which removes the message framing.
- The tool definitions the server sends on every turn: count_tokens with the eight
  tools minus without them.

Prints a Markdown table (chars, estimate, measured, chars per token). The estimate
is meant to overestimate: every ratio should be 3.0 or more (ADR-005).
"""

import argparse
import asyncio
import math
import os
import shutil
import statistics
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # for tests.*

from compass.indexer.pipeline import index_repo  # noqa: E402
from compass.store.db import open_index  # noqa: E402
from compass.tools.deps import module_dependencies  # noqa: E402
from compass.tools.hotspots import hotspots  # noqa: E402
from compass.tools.impact import diff_impact  # noqa: E402
from compass.tools.outline import file_outline  # noqa: E402
from compass.tools.references import find_references  # noqa: E402
from compass.tools.render import estimate_tokens  # noqa: E402
from compass.tools.summary import repo_summary  # noqa: E402
from compass.tools.symbols import get_symbol, search_symbols  # noqa: E402

DEFAULT_MODEL = "claude-opus-5-5"


def _samples(work: Path, repo: Path | None) -> list[tuple[str, str]]:
    """(label, tool answer) pairs to measure."""
    from tests.gitrepo import ScriptedRepo
    from tests.helpers import FIXTURES
    from tests.shop_history import shop_history

    samples = []
    index_repo(FIXTURES, work / "fixtures.db")
    conn = open_index(work / "fixtures.db")
    try:
        samples += [
            ("repo_summary fixtures", repo_summary(conn, "fresh", 10)),
            ("search_symbols save", search_symbols(conn, "save", None, 10)),
            ("search_symbols list model/", search_symbols(conn, "", None, 40, "model/")),
            ("get_symbol Order.addAll", get_symbol(conn, "Order.addAll", 60)),
            ("find_references Order.java:25", find_references(conn, "Order.java:25", 10)),
            ("file_outline Order.java", file_outline(conn, "Order.java", 30)),
            ("module_dependencies", module_dependencies(conn, None, 10)),
        ]
    finally:
        conn.close()

    os.environ["GIT_CONFIG_GLOBAL"] = os.devnull  # a stable scripted repo, as in the tests
    os.environ["GIT_CONFIG_NOSYSTEM"] = "1"
    shop = shop_history(ScriptedRepo(work / "shop"))
    index_repo(shop.root, work / "shop.db")
    conn = open_index(work / "shop.db")
    try:
        samples += [
            ("diff_impact main...HEAD", diff_impact(conn, "main", "HEAD", 10)),
            ("hotspots shop", hotspots(conn, "2026-01-01", 10, False)),
        ]
    finally:
        conn.close()
    del os.environ["GIT_CONFIG_GLOBAL"], os.environ["GIT_CONFIG_NOSYSTEM"]

    if repo is not None:
        index_repo(repo, work / "repo.db")
        conn = open_index(work / "repo.db")
        try:
            samples += [
                (f"repo_summary {repo.name}", repo_summary(conn, "fresh", 10)),
                (f"module_dependencies {repo.name}", module_dependencies(conn, None, 30)),
                (f"hotspots {repo.name}", hotspots(conn, None, 30, False)),
            ]
        finally:
            conn.close()
    return samples


async def _tool_definitions() -> list[dict]:
    from mcp import Client

    from compass.server import build_server
    from tests.helpers import FIXTURES

    with tempfile.TemporaryDirectory() as tmp:
        async with Client(build_server(FIXTURES, Path(tmp) / "i.db"), mode="legacy") as client:
            listed = (await client.list_tools()).tools
    return [
        {"name": t.name, "description": t.description or "", "input_schema": t.input_schema}
        for t in listed
    ]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--repo", type=Path, help="also measure answers on this repository")
    parser.add_argument("--model", default=os.environ.get("COMPASS_MODEL", DEFAULT_MODEL))
    args = parser.parse_args()

    if os.environ.get("CI"):
        sys.exit("calibrate_tokens.py calls the Claude API and is manual only; not run in CI.")
    try:
        import anthropic
    except ImportError:
        sys.exit("The anthropic SDK is an optional extra: run `uv sync --extra agent` first.")

    client = anthropic.Anthropic()

    def count(text: str, tools: list[dict] | None = None) -> int:
        extra = {"tools": tools} if tools else {}
        return client.messages.count_tokens(
            model=args.model, messages=[{"role": "user", "content": text}], **extra
        ).input_tokens

    work = Path(tempfile.mkdtemp(prefix="compass-calibrate-"))
    try:
        samples = _samples(work, args.repo.resolve() if args.repo else None)
    finally:
        shutil.rmtree(work, ignore_errors=True)

    baseline = count(".")
    print(f"model {args.model}; framing baseline {baseline} tokens subtracted from each count\n")
    print("| Answer | Chars | Estimate (chars/3) | Measured | Chars per token |")
    print("|---|---|---|---|---|")
    ratios = []
    for label, text in samples:
        measured = count(text) - baseline + 1  # the baseline message holds one character
        ratio = len(text) / measured
        ratios.append(ratio)
        print(f"| {label} | {len(text)} | {estimate_tokens(text)} | {measured} | {ratio:.2f} |")

    tools = asyncio.run(_tool_definitions())
    listed = count(".", tools) - baseline
    chars = sum(len(t["name"] + t["description"]) for t in tools)
    print(f"\ntool definitions ({len(tools)} tools): {listed} tokens measured")
    print(f"(names + descriptions {chars} chars, about {math.ceil(chars / 3)} by the estimate;")
    print(" the measured count also includes the input schemas and the API's tool framing)")
    print(
        f"\nchars per token: min {min(ratios):.2f}, median {statistics.median(ratios):.2f},"
        f" max {max(ratios):.2f}. The estimate overestimates every answer when min >= 3.0."
    )


if __name__ == "__main__":
    main()
