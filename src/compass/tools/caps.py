"""Output limits shared by every tool.

CAPS are hard ceilings on the size of one tool response, in estimated tokens
(see render.estimate_tokens). The renderer stops adding results before a cap is
reached, so no `limit` value can push a response past it. tests/test_token_caps.py
enforces them on a stress repository, and docs/tools.md documents them; a test
checks that the two tables agree.
"""

CAPS = {
    "repo_summary": 600,
    "search_symbols": 1500,
    "get_symbol": 2000,
    "find_references": 2000,
    "file_outline": 1500,
    "module_dependencies": 1200,
    "diff_impact": 2000,
    "hotspots": 1200,
}

# The review agent's baseline tools (tools/baseline.py): plain file and text access,
# held to the same discipline so the comparison in M5 is about what the tools know,
# not about how much they are allowed to say.
BASELINE_CAPS = {
    "list_files": 1500,
    "read_file": 2000,
    "grep": 2000,
    "git_diff": 2000,
}
BASELINE_TOOLS_LIST_CAP = 800

# read_file window, in lines; list_files limit; git_diff context lines.
DEFAULT_READ_LINES = 200
MAX_READ_LINES = 400
DEFAULT_GREP_LIMIT = 50
DEFAULT_FILES_LIMIT = 100
MAX_FILES_LIMIT = 500
MAX_DIFF_CONTEXT = 10

# A response that is only a status or error message (no index yet, unknown symbol, ...).
STATUS_CAP = 200

# All tool names, descriptions and input schemas together. The client sends these to
# the model on every turn, so they cost tokens even when no tool is called.
TOOLS_LIST_CAP = 1500

DEFAULT_LIMIT = 10
MAX_LIMIT = 200

# get_symbol body length, in source lines.
DEFAULT_BODY_LINES = 60
MAX_BODY_LINES = 400
