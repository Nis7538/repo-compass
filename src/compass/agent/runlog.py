"""Where the review agent writes its run log and transcripts.

One JSON object per line per run, appended, for the M5 eval harness. Both files
default to the per-user cache directory, never the target repository: transcripts
hold repository content (diffs, file text, tool answers) and must not end up
committed anywhere by accident.
"""

import json
from pathlib import Path

from compass.paths import cache_dir

SCHEMA_VERSION = 1


def default_log_path() -> Path:
    return cache_dir() / "runs.jsonl"


def default_transcript_path(run_id: str) -> Path:
    return cache_dir() / "transcripts" / f"{run_id}.json"


def append_record(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def write_transcript(path: Path, transcript: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(transcript, ensure_ascii=False, indent=1), encoding="utf-8")


def inside(path: Path, root: Path) -> bool:
    """True if path is root or lies under it (after resolving both)."""
    return path.resolve().is_relative_to(root.resolve())
