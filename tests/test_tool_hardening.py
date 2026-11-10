"""delete_memory cleans Mem0's history, refuses malformed ids; add_memory warns
when nothing was stored.

Findings of the first real-stack smoke test (Mem0 2.0.20, Qdrant 1.19.0):

- `Memory.delete` leaves the ADD and DELETE history rows (full text) in
  history.db, and a later `erase` only collects ids of LIVE memories, so that
  text could never be purged afterwards.
- A malformed id reached Qdrant, which answered 400; the tool returned
  `internal error` and logged a full traceback.
- An empty LLM reply makes Mem0 store nothing, and the tool answered
  `{"stored": 0}` with no hint that the text was lost.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import replace

import pytest

import mcp_server
import memory
from tests.fakes import FakeMemory


class _HistoryMemory(FakeMemory):
    """FakeMemory that writes history rows the way Mem0 2.0.20 does, through
    Mem0's own SQLiteManager: ADD on add, DELETE (old text kept) on delete."""

    def __init__(self, db_path: str):
        super().__init__()
        from mem0.memory.storage import SQLiteManager

        self.db = SQLiteManager(db_path)

    def add(self, messages, *, user_id=None, **kwargs):
        out = super().add(messages, user_id=user_id, **kwargs)
        for r in out["results"]:
            self.db.add_history(r["id"], None, r["memory"], "ADD", actor_id=user_id)
        return out

    def delete(self, memory_id):
        old = self.get(memory_id)
        out = super().delete(memory_id)
        self.db.add_history(memory_id, old["memory"], None, "DELETE", is_deleted=1)
        return out


def _rows(path, sql, params=()):
    con = sqlite3.connect(str(path))
    try:
        return con.execute(sql, params).fetchall()
    finally:
        con.close()


@pytest.fixture
def as_user():
    tokens = []

    def _set(user_id):
        tokens.append(mcp_server.current_user_id.set(user_id))

    yield _set
    for t in reversed(tokens):
        mcp_server.current_user_id.reset(t)


@pytest.fixture
def history(tmp_path, monkeypatch):
    db = tmp_path / "history.db"
    monkeypatch.setattr(mcp_server, "config", replace(mcp_server.config, history_db=str(db)))
    fake = _HistoryMemory(str(db))
    monkeypatch.setattr(mcp_server, "_mem0", lambda: fake)
    yield db, fake
    fake.db.connection.close()


def _add(as_user, user, text):
    as_user(user)
    return json.loads(mcp_server.add_memory(text))["results"][0]["id"]


# --- F4: delete_memory purges the deleted memory's history ------------------


def test_delete_memory_purges_its_history_rows_only(history, as_user):
    db, _fake = history
    a1 = _add(as_user, "alice", "alice takes metformin")
    a2 = _add(as_user, "alice", "alice lives in a flat")
    b1 = _add(as_user, "bob", "bob plays cello")

    as_user("alice")
    out = json.loads(mcp_server.delete_memory(a1))

    assert out == {"deleted": True, "id": a1}
    assert _rows(db, "SELECT count(*) FROM history WHERE memory_id = ?", (a1,)) == [(0,)]
    assert b"metformin" not in b"".join(str(r).encode() for r in _rows(db, "SELECT * FROM history"))
    left = {r[0] for r in _rows(db, "SELECT memory_id FROM history")}
    assert left == {a2, b1}


def test_delete_memory_purge_failure_is_reported_not_hidden(history, as_user, caplog, monkeypatch):
    _db, fake = history
    a1 = _add(as_user, "alice", "alice takes metformin")

    def broken(db_path, ids):
        raise sqlite3.OperationalError("database is locked /secret/path")

    monkeypatch.setattr(memory, "purge_history", broken)
    as_user("alice")
    with caplog.at_level(logging.ERROR, logger="memsilo.tools"):
        raw = mcp_server.delete_memory(a1)

    out = json.loads(raw)
    assert out["deleted"] is True and out["id"] == a1
    assert "error" in out and "cleanup" in out["error"]
    assert "secret" not in raw and "locked" not in raw
    assert fake.get(a1) is None  # the delete itself did happen
    assert any(r.exc_info for r in caplog.records)


def test_delete_memory_rows_left_after_purge_is_reported(history, as_user, monkeypatch):
    _db, _fake = history
    a1 = _add(as_user, "alice", "alice takes metformin")
    monkeypatch.setattr(memory, "purge_history", lambda db_path, ids: 0)  # deletes nothing
    as_user("alice")
    out = json.loads(mcp_server.delete_memory(a1))
    assert out["deleted"] is True
    assert "cleanup" in out.get("error", "")


def test_refused_delete_touches_no_history(history, as_user):
    db, _fake = history
    a1 = _add(as_user, "alice", "alice takes metformin")
    as_user("bob")
    out = json.loads(mcp_server.delete_memory(a1))
    assert out == {"deleted": False, "error": "not found or not owned by you"}
    assert _rows(db, "SELECT count(*) FROM history WHERE memory_id = ?", (a1,)) == [(1,)]


# --- F7: a malformed id never reaches the backend ----------------------------


class _Recording:
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        def record(*args, **kwargs):
            self.calls.append(name)
            raise AssertionError(f"backend called: {name}")

        return record


_UUID = "3f2b8c1e-7a4d-4e5f-9b6a-1c2d3e4f5a6b"  # canonical form, as Mem0 mints them


@pytest.mark.parametrize(
    "bad",
    [
        "not-a-uuid",
        "*",
        "",
        "1",
        "../etc",
        _UUID.upper(),
        "{" + _UUID + "}",
        _UUID.replace("-", ""),
        _UUID + " ",
        "urn:uuid:" + _UUID,
    ],
)
def test_delete_memory_malformed_id_is_not_found(monkeypatch, as_user, caplog, bad):
    rec = _Recording()
    monkeypatch.setattr(mcp_server, "_mem0", lambda: rec)
    as_user("alice")
    with caplog.at_level(logging.WARNING, logger="memsilo.tools"):
        out = json.loads(mcp_server.delete_memory(bad))
    assert out == {"deleted": False, "error": "not found or not owned by you"}
    assert rec.calls == []
    assert not any(r.exc_info for r in caplog.records)


# --- F5: nothing stored is said, not silent -----------------------------------


class _AddsNothing:
    def add(self, *args, **kwargs):
        return {"results": []}


def test_add_memory_warns_when_nothing_was_stored(monkeypatch, as_user, caplog):
    monkeypatch.setattr(mcp_server, "_mem0", lambda: _AddsNothing())
    as_user("alice")
    text = "alice says the zebra code is 4242"
    with caplog.at_level(logging.WARNING, logger="memsilo.tools"):
        out = json.loads(mcp_server.add_memory(text))
    assert out["stored"] == 0
    assert out["results"] == []
    assert "error" not in out
    assert "nothing was stored" in out["warning"]
    assert "reasoning" in out["warning"]
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings, "a server-side warning is logged"
    logged = " ".join(r.getMessage() for r in caplog.records)
    assert "zebra" not in logged and "4242" not in logged


def test_add_memory_with_results_has_no_warning(monkeypatch, as_user):
    fake = FakeMemory()
    monkeypatch.setattr(mcp_server, "_mem0", lambda: fake)
    as_user("alice")
    out = json.loads(mcp_server.add_memory("alice likes tea"))
    assert out["stored"] == 1
    assert "warning" not in out
