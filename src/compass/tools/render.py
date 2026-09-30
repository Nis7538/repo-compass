"""Compact text output under a hard token budget.

Every tool answers in plain text: one result per line, grouped by file where
that helps, no JSON and no repeated keys. The functions here do the layout and
make sure a response never exceeds its cap (tools/caps.py):

- results arrive already ranked; `fit_*` keeps the longest ranked prefix that
  fits, so what gets cut is always the lowest-ranked tail;
- room is always reserved for a `[truncated: N more]` marker and for the
  server's one-line index status, so neither can push a response over its cap;
- `enforce_cap` is a last-resort guard applied to every finished response.

Tokens are estimated as ceil(chars / 3). There is no offline Claude tokenizer;
code, paths and camelCase names tokenize at roughly 3-4 characters per token, so
dividing by 3 errs on the side of overestimating (see docs/adr/005).
"""

from dataclasses import dataclass

CHARS_PER_TOKEN = 3

# Characters kept free in every response: ~300 for the truncation marker and
# ~200 for the index status line the server may prepend.
MARKER_WIDTH = 300
STATUS_WIDTH = 200
HEADROOM = MARKER_WIDTH + STATUS_WIDTH

ELLIPSIS = "…"


def estimate_tokens(text: str) -> int:
    return -(-len(text) // CHARS_PER_TOKEN)


def clip(text: str, width: int) -> str:
    """Collapse whitespace to single spaces and cut to width characters."""
    text = " ".join(text.split())
    return text if len(text) <= width else text[: width - 1] + ELLIPSIS


def truncated(
    count: int, detail: str | None = None, hint: str | None = None, unit: str | None = None
) -> str:
    """'[truncated: 30 more (likely 27, possible 3)] limit=40 shows all'."""
    marker = f"[truncated: {count} more" + (f" {unit}" if unit else "")
    marker += (f" ({detail})" if detail else "") + "]"
    return clip(marker + (f" {hint}" if hint else ""), MARKER_WIDTH)


def common_dir(paths: list[str]) -> str:
    """Directory prefix shared by all paths ('src/main/java/com/x/'), or ''.

    Only worth printing when there are at least two distinct paths and the shared
    part has at least two directory segments.
    """
    distinct = sorted(set(paths))
    if len(distinct) < 2:
        return ""
    common: list[str] = []
    for parts in zip(*(p.split("/")[:-1] for p in distinct)):
        if len(set(parts)) != 1:
            break
        common.append(parts[0])
    return "/".join(common) + "/" if len(common) >= 2 else ""


@dataclass(frozen=True)
class Entry:
    group: str  # repo-relative file path
    text: str  # one line, without indentation


def _budget(head: list[str], cap_tokens: int) -> int:
    return cap_tokens * CHARS_PER_TOKEN - HEADROOM - sum(len(line) + 1 for line in head)


def fit_lines(head: list[str], lines: list[str], cap_tokens: int) -> tuple[list[str], int]:
    """head + the longest prefix of lines that fits. Returns (lines, how many fitted)."""
    budget = _budget(head, cap_tokens)
    shown = 0
    for line in lines:
        budget -= len(line) + 1
        if budget < 0:
            break
        shown += 1
    return head + lines[:shown], shown


def fit_grouped(head: list[str], entries: list[Entry], cap_tokens: int) -> tuple[list[str], int]:
    """Lay out ranked entries grouped by file. Returns (lines, how many entries fitted).

    Each file path is printed once, followed by its entries indented two spaces.
    Files appear in the order of their best-ranked entry; inside a file, entries
    keep their rank order. When the files share a directory prefix it is printed
    once as 'paths under <prefix>' and dropped from each file line.
    """
    prefix = common_dir([e.group for e in entries])
    prefix_line = [f"paths under {prefix}"] if prefix else []
    budget = _budget(head + prefix_line, cap_tokens)

    groups: dict[str, list[str]] = {}
    shown = 0
    for entry in entries:
        cost = len(entry.text) + 3
        if entry.group not in groups:
            cost += len(entry.group) - len(prefix) + 1
        budget -= cost
        if budget < 0:
            break
        groups.setdefault(entry.group, []).append(entry.text)
        shown += 1

    lines = head + (prefix_line if groups else [])
    for group, texts in groups.items():
        lines.append(group[len(prefix) :])
        lines.extend("  " + text for text in texts)
    return lines, shown


def more_hint(total: int, shown: int, limit: int, max_limit: int) -> str:
    """What to do about a cut: raise the limit, or narrow the query if the cap cut it."""
    if shown < limit:
        return "output cap reached; narrow the query"
    if total <= max_limit:
        return f"limit={total} shows all"
    return f"limit={max_limit} shows more"


def enforce_cap(text: str, cap_tokens: int) -> str:
    """Last-resort guard: cut whole lines from the end until text fits the cap."""
    limit = cap_tokens * CHARS_PER_TOKEN
    if len(text) <= limit:
        return text
    marker = "[truncated: output cap reached]"
    kept: list[str] = []
    used = len(marker)
    for line in text.split("\n"):
        used += len(line) + 1
        if used > limit:
            break
        kept.append(line)
    return "\n".join(kept + [marker])
