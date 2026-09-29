"""SQLite schema for the index.

Every row is owned by a row in `files` and removed with it (ON DELETE CASCADE),
so reindexing a file is "delete its files row, insert fresh rows".

Reference resolution is deliberately NOT stored: `refs` holds only what the
parser saw (callee name, receiver text, location). Resolution happens at query
time (see store/resolve.py and docs/adr/003-reference-resolution.md), which is
why changing one file never requires rewriting rows that belong to another.

`references` is an SQL keyword, hence the table name `refs`.
"""

# Bump when the schema changes. A mismatch drops and rebuilds the index;
# the index is a cache, so there are no migrations.
SCHEMA_VERSION = 1

TABLES = ["symbols_fts", "refs", "imports", "symbols", "files", "meta"]
TRIGGERS = ["symbols_fts_insert", "symbols_fts_delete"]

DDL = """
CREATE TABLE meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE files (
    id          INTEGER PRIMARY KEY,
    path        TEXT NOT NULL UNIQUE,   -- repo-relative, forward slashes
    language    TEXT NOT NULL,          -- 'java' | 'python'
    module      TEXT NOT NULL,          -- Java package / Python dotted module
    hash        TEXT NOT NULL,          -- sha256 of the file bytes
    size        INTEGER NOT NULL,
    mtime_ns    INTEGER NOT NULL,
    line_count  INTEGER NOT NULL,
    has_errors  INTEGER NOT NULL        -- tree-sitter reported syntax errors
);

CREATE TABLE symbols (
    id              INTEGER PRIMARY KEY,
    file_id         INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    parent_id       INTEGER REFERENCES symbols(id) ON DELETE CASCADE,
    kind            TEXT NOT NULL,
    name            TEXT NOT NULL,
    qualified_name  TEXT NOT NULL,
    signature       TEXT NOT NULL,
    param_count     INTEGER,
    is_varargs      INTEGER NOT NULL DEFAULT 0,
    decorators      TEXT NOT NULL DEFAULT '[]',   -- JSON list
    bases           TEXT NOT NULL DEFAULT '[]',   -- JSON list
    doc             TEXT,
    name_words      TEXT NOT NULL,                -- 'getUserById' -> 'get user by id'
    start_line      INTEGER NOT NULL,
    end_line        INTEGER NOT NULL
);

CREATE TABLE imports (
    id         INTEGER PRIMARY KEY,
    file_id    INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    module     TEXT NOT NULL,
    name       TEXT,
    alias      TEXT,
    is_static  INTEGER NOT NULL DEFAULT 0,
    line       INTEGER NOT NULL
);

CREATE TABLE refs (
    id                   INTEGER PRIMARY KEY,
    file_id              INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    enclosing_symbol_id  INTEGER REFERENCES symbols(id) ON DELETE CASCADE,
    kind                 TEXT NOT NULL,
    name                 TEXT NOT NULL,
    receiver             TEXT,
    arg_count            INTEGER,
    line                 INTEGER NOT NULL,
    col                  INTEGER NOT NULL
);

CREATE INDEX idx_symbols_file ON symbols(file_id);
CREATE INDEX idx_symbols_parent ON symbols(parent_id);
CREATE INDEX idx_symbols_name ON symbols(name);
CREATE INDEX idx_symbols_qualified ON symbols(qualified_name);
CREATE INDEX idx_imports_file ON imports(file_id);
CREATE INDEX idx_imports_module ON imports(module, name);
CREATE INDEX idx_refs_file ON refs(file_id);
CREATE INDEX idx_refs_enclosing ON refs(enclosing_symbol_id);
CREATE INDEX idx_refs_name ON refs(name);

-- Full-text index over symbol names, kept in sync with `symbols` by triggers.
CREATE VIRTUAL TABLE symbols_fts USING fts5(
    name, qualified_name, name_words,
    content='symbols', content_rowid='id'
);

CREATE TRIGGER symbols_fts_insert AFTER INSERT ON symbols BEGIN
    INSERT INTO symbols_fts(rowid, name, qualified_name, name_words)
    VALUES (new.id, new.name, new.qualified_name, new.name_words);
END;

CREATE TRIGGER symbols_fts_delete AFTER DELETE ON symbols BEGIN
    INSERT INTO symbols_fts(symbols_fts, rowid, name, qualified_name, name_words)
    VALUES ('delete', old.id, old.name, old.qualified_name, old.name_words);
END;
"""
