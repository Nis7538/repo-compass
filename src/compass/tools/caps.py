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

# A response that is only a status or error message (no index yet, unknown symbol, ...).
STATUS_CAP = 200

# All tool names, descriptions and input schemas together. The client sends these to
# the model on every turn, so they cost tokens even when no tool is called.
TOOLS_LIST_CAP = 1200

DEFAULT_LIMIT = 10
MAX_LIMIT = 200

# get_symbol body length, in source lines.
DEFAULT_BODY_LINES = 60
MAX_BODY_LINES = 400
