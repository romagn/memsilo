import json

import pytest

import mcp_server
from users import UserStore


class _FakeMem0:
    def __init__(self):
        self.store = {}  # user_id -> list[dict]

    def add(self, text, user_id, metadata):
        entry = {"id": f"{user_id}-{len(self.store.get(user_id, []))}", "memory": text}
        self.store.setdefault(user_id, []).append(entry)
        return {"results": [entry]}

    def get_all(self, filters, top_k):
        user_id = filters["user_id"]
        return {"results": self.store.get(user_id, [])[:top_k]}

    def search(self, query, filters, top_k):
        user_id = filters["user_id"]
        return {"results": [{**e, "score": 1.0} for e in self.store.get(user_id, [])][:top_k]}


@pytest.fixture(autouse=True)
def fake_mem0(monkeypatch):
    fake = _FakeMem0()
    monkeypatch.setattr(mcp_server, "_memory", fake)
    monkeypatch.setattr(mcp_server, "_mem0", lambda: fake)
    return fake


@pytest.fixture
def store(tmp_path, monkeypatch):
    s = UserStore(str(tmp_path / "users.json"))
    monkeypatch.setattr(mcp_server, "_user_store", s)
    return s


def test_add_then_search_is_scoped_to_caller(store):
    store.add_user("alice", "Alice")
    store.add_user("bob", "Bob")

    # This test drives the tool functions directly under a contextvar set the
    # same way the middleware sets it, rather than speaking the full MCP JSON-RPC
    # wire protocol — the middleware behavior itself is covered end-to-end in
    # tests/test_auth.py; this test's job is confirming tool functions actually
    # call resolve_user_id() rather than a hardcoded/default user.
    tok = mcp_server.current_user_id.set("alice")
    try:
        mcp_server.add_memory("alice's fact")
    finally:
        mcp_server.current_user_id.reset(tok)

    # Verify alice can see her own memory
    tok = mcp_server.current_user_id.set("alice")
    try:
        result = json.loads(mcp_server.get_all_memories())
    finally:
        mcp_server.current_user_id.reset(tok)

    assert len(result) == 1  # alice sees her own memory
    assert result[0]["memory"] == "alice's fact"

    # Verify bob cannot see alice's memory
    tok = mcp_server.current_user_id.set("bob")
    try:
        result = json.loads(mcp_server.get_all_memories())
    finally:
        mcp_server.current_user_id.reset(tok)

    assert result == []  # bob sees none of alice's memories


def test_search_memory_survives_a_null_score(store, monkeypatch):
    """`.get("score", 0)` only defaults a *missing* key — an explicit None
    reached round() and raised TypeError, crashing the tool."""

    class _NullScoreMem0:
        def search(self, query, filters, top_k):
            return {"results": [{"memory": "m", "score": None}, {"memory": "n"}]}

    monkeypatch.setattr(mcp_server, "_mem0", lambda: _NullScoreMem0())
    tok = mcp_server.current_user_id.set("alice")
    try:
        result = json.loads(mcp_server.search_memory("anything"))
    finally:
        mcp_server.current_user_id.reset(tok)
    assert [r["score"] for r in result] == [0, 0]
