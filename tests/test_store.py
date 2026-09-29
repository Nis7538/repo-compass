"""Schema, FTS sync, cascade deletes and schema-version handling."""

import pytest

from compass.indexer.models import FileExtract, Import, Ref, Symbol
from compass.paths import default_db_path
from compass.store.db import (
    FileRecord,
    IndexNotFoundError,
    connect,
    delete_file,
    load_file_states,
    open_index,
    set_meta,
    split_words,
    write_file,
)


def _extract() -> FileExtract:
    cls = Symbol("class", "OrderService", "shop.OrderService", "class OrderService", 1, 20)
    method = Symbol(
        "method", "placeOrder", "shop.OrderService.placeOrder", "void placeOrder()", 3, 9, 0
    )
    return FileExtract(
        module="shop",
        symbols=[cls, method],
        imports=[Import("shop.model", "Order", None, 1)],
        refs=[Ref("new", "Order", None, 0, 4, 9, 1)],
    )


def _record(path: str = "shop/OrderService.java") -> FileRecord:
    return FileRecord(path, "java", "abc", 100, 1, 20)


def _count(conn, table: str) -> int:
    return conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]


def test_write_file_stores_rows_with_parent_links(tmp_path):
    conn = connect(tmp_path / "i.db")
    with conn:
        write_file(conn, _record(), _extract())
    method = conn.execute("SELECT * FROM symbols WHERE name = 'placeOrder'").fetchone()
    parent = conn.execute("SELECT * FROM symbols WHERE id = ?", (method["parent_id"],)).fetchone()
    assert parent["name"] == "OrderService"
    ref = conn.execute("SELECT * FROM refs").fetchone()
    assert ref["enclosing_symbol_id"] == method["id"]
    assert load_file_states(conn)["shop/OrderService.java"].module == "shop"


def test_fts_follows_inserts_and_deletes(tmp_path):
    conn = connect(tmp_path / "i.db")
    with conn:
        file_id = write_file(conn, _record(), _extract())
    hits = conn.execute(
        "SELECT rowid FROM symbols_fts WHERE symbols_fts MATCH ?", ('"place" "order"',)
    ).fetchall()
    assert len(hits) == 1  # found via the split name_words column
    with conn:
        delete_file(conn, file_id)
    hits = conn.execute(
        "SELECT rowid FROM symbols_fts WHERE symbols_fts MATCH 'placeOrder'"
    ).fetchall()
    assert hits == []


def test_deleting_file_cascades(tmp_path):
    conn = connect(tmp_path / "i.db")
    with conn:
        file_id = write_file(conn, _record(), _extract())
        write_file(conn, _record("other.java"), _extract())
        delete_file(conn, file_id)
    assert _count(conn, "files") == 1
    assert _count(conn, "symbols") == 2
    assert _count(conn, "imports") == 1
    assert _count(conn, "refs") == 1


def test_write_file_replaces_previous_rows(tmp_path):
    conn = connect(tmp_path / "i.db")
    with conn:
        write_file(conn, _record(), _extract())
        write_file(conn, _record(), _extract())
    assert _count(conn, "files") == 1
    assert _count(conn, "symbols") == 2


def test_schema_version_mismatch_rebuilds(tmp_path):
    db = tmp_path / "i.db"
    conn = connect(db)
    with conn:
        write_file(conn, _record(), _extract())
        set_meta(conn, "schema_version", "0")
    conn.close()
    with pytest.raises(IndexNotFoundError):
        open_index(db)
    conn = connect(db)
    assert _count(conn, "files") == 0


def test_open_index_requires_existing_db(tmp_path):
    with pytest.raises(IndexNotFoundError):
        open_index(tmp_path / "missing.db")
    assert not (tmp_path / "missing.db").exists()


def test_default_db_path_is_outside_repo(tmp_path):
    repo = tmp_path / "myrepo"
    repo.mkdir()
    db = default_db_path(repo)
    assert db.name.startswith("myrepo-") and db.suffix == ".db"
    assert repo.resolve() not in db.resolve().parents
    assert default_db_path(repo) == db  # stable


@pytest.mark.parametrize(
    ("name", "words"),
    [
        ("getUserById", "get user by id"),
        ("parse_http_url", "parse http url"),
        ("HTTPServer", "http server"),
        ("OrderService", "order service"),
        ("v2Api", "v 2 api"),
    ],
)
def test_split_words(name, words):
    assert split_words(name) == words
