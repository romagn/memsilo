"""One customer must never see or delete another's memories.

Two users, two tokens, the real streamable-HTTP app and auth middleware, and a
single shared FakeMemory (mem0 2.0.20-faithful) behind both.
"""

from __future__ import annotations

import json

import pytest
from starlette.testclient import TestClient

import mcp_server
import memory
from config import Config, load_config
from tests.fakes import FakeMemory
from users import UserStore

_BASE = {"accept": "application/json, text/event-stream", "content-type": "application/json"}


@pytest.fixture
def shared(monkeypatch):
    fake = FakeMemory()
    monkeypatch.setattr(mcp_server, "_memory", fake)
    monkeypatch.setattr(mcp_server, "_mem0", lambda: fake)
    return fake


@pytest.fixture
def world(tmp_path, monkeypatch, shared):
    store = UserStore(str(tmp_path / "users.json"))
    monkeypatch.setattr(mcp_server, "_user_store", store)
    tokens = {"alice": store.add_user("alice", "Alice"), "bob": store.add_user("bob", "Bob")}
    app = mcp_server.mcp.streamable_http_app(host="0.0.0.0")
    app.add_middleware(
        mcp_server.build_auth_middleware(store, mcp_server.SessionRegistry.from_app(app))
    )
    with TestClient(app) as client:
        sessions = {}
        for name, token in tokens.items():
            h = {**_BASE, "authorization": f"Bearer {token}"}
            r = client.post(
                "/mcp",
                headers=h,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {},
                        "clientInfo": {"name": "pytest", "version": "1"},
                    },
                },
            )
            assert r.status_code == 200, r.text
            h["mcp-session-id"] = r.headers["mcp-session-id"]
            n = client.post(
                "/mcp",
                headers=h,
                json={"jsonrpc": "2.0", "method": "notifications/initialized"},
            )
            assert n.status_code == 202, n.text
            sessions[name] = h

        def call(user: str, tool: str, **arguments):
            r = client.post(
                "/mcp",
                headers=sessions[user],
                json={
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {"name": tool, "arguments": arguments},
                },
            )
            assert r.status_code == 200, r.text
            for line in r.text.splitlines():
                if line.startswith("data: "):
                    result = json.loads(line[6:])["result"]
                    assert not result.get("isError"), result
                    return json.loads(result["structuredContent"]["result"])
            raise AssertionError(r.text)

        yield call


def _ids(items):
    return {i["id"] for i in items}


def test_search_never_returns_other_users_memories(world):
    world("alice", "add_memory", text="alice secret")
    world("bob", "add_memory", text="bob secret")
    assert [r["memory"] for r in world("alice", "search_memory", query="secret")] == [
        "alice secret"
    ]
    assert [r["memory"] for r in world("bob", "search_memory", query="secret")] == ["bob secret"]


def test_get_all_lists_only_own_memories(world):
    world("alice", "add_memory", text="a1")
    world("alice", "add_memory", text="a2")
    world("bob", "add_memory", text="b1")
    assert sorted(m["memory"] for m in world("alice", "get_all_memories")) == ["a1", "a2"]
    assert [m["memory"] for m in world("bob", "get_all_memories")] == ["b1"]


def test_memory_stats_counts_only_own_memories(world):
    for i in range(3):
        world("alice", "add_memory", text=f"a{i}")
    for i in range(2):
        world("bob", "add_memory", text=f"b{i}")
    a, b = world("alice", "memory_stats"), world("bob", "memory_stats")
    assert (a["user_id"], a["total_memories"]) == ("alice", 3)
    assert (b["user_id"], b["total_memories"]) == ("bob", 2)
    assert all(s.startswith("b") for s in b["recent_sample"])


def test_delete_other_users_memory_id_is_refused_and_memory_survives(world, shared):
    world("alice", "add_memory", text="alice only")
    alice_id = world("alice", "get_all_memories")[0]["id"]
    out = world("bob", "delete_memory", memory_id=alice_id)
    assert out["deleted"] is False
    assert shared.get(alice_id) is not None
    assert [m["memory"] for m in world("alice", "get_all_memories")] == ["alice only"]


def test_erasing_alice_leaves_bob_untouched(world, shared):
    for i in range(3):
        world("alice", "add_memory", text=f"a{i}")
    for i in range(2):
        world("bob", "add_memory", text=f"b{i}")
    before = _ids(world("bob", "get_all_memories"))
    assert memory.erase_user(shared, "alice") == 3
    assert world("alice", "get_all_memories") == []
    assert _ids(world("bob", "get_all_memories")) == before
    assert world("bob", "memory_stats")["total_memories"] == 2


def test_metadata_cannot_override_user_id(world, shared):
    """add_memory always passes the authenticated user_id as its own argument
    and never lets caller metadata choose the owner (reserved keys are now
    rejected outright, see test_reserved_metadata_keys_are_rejected). This does
    NOT exercise mem0's own identity-key strip."""
    out = world("alice", "add_memory", text="hello", metadata=json.dumps({"source": "sms"}))
    assert out["stored"] == 1
    assert world("bob", "get_all_memories") == []
    assert set(shared.store) == {"alice"}
    assert shared.store["alice"][0]["user_id"] == "alice"
    assert shared.store["alice"][0]["metadata"] == {"source": "sms"}


@pytest.mark.parametrize(
    "key", ["user_id", "agent_id", "run_id", "actor_id", "User_Id", " user_id ", "RUN_ID"]
)
def test_reserved_metadata_keys_are_rejected(world, shared, key):
    out = world("alice", "add_memory", text="x", metadata=json.dumps({key: "SECRETVALUE"}))
    assert out["stored"] == 0
    assert key.strip().lower() in out["error"].lower()
    assert "SECRETVALUE" not in json.dumps(out)
    assert shared.store == {}


@pytest.mark.parametrize("raw", ["[1]", '"str"', "5", "null", "true"])
def test_non_object_metadata_is_rejected(world, shared, raw):
    out = world("alice", "add_memory", text="x", metadata=raw)
    assert out["stored"] == 0
    assert "object" in out["error"]
    assert shared.store == {}


@pytest.fixture
def spy(monkeypatch):
    class Spy(FakeMemory):
        def __init__(self):
            super().__init__()
            self.top_ks = []

        def search(self, query, *, top_k=20, filters=None, **kw):
            self.top_ks.append(top_k)
            return super().search(query, top_k=top_k, filters=filters, **kw)

        def get_all(self, *, filters=None, top_k=20, **kw):
            self.top_ks.append(top_k)
            return super().get_all(filters=filters, top_k=top_k, **kw)

    fake = Spy()
    monkeypatch.setattr(mcp_server, "_mem0", lambda: fake)
    return fake


@pytest.mark.parametrize("tool", ["search_memory", "get_all_memories"])
@pytest.mark.parametrize("limit,expected", [(0, 1), (-5, 1), (10**9, 200), (200, 200), (7, 7)])
def test_limit_is_clamped(world, spy, tool, limit, expected):
    for i in range(205):
        spy.add(f"f{i}", user_id="alice")
    args = {"query": "f"} if tool == "search_memory" else {}
    out = world("alice", tool, limit=limit, **args)
    assert spy.top_ks == [expected]
    assert len(out) == expected


def test_search_limit_is_honoured(world):
    for i in range(5):
        world("alice", "add_memory", text=f"fact {i}")
    assert len(world("alice", "search_memory", query="fact", limit=2)) == 2


def test_default_host_is_loopback():
    assert load_config(env={"ANTHROPIC_API_KEY": "k"}).host == "127.0.0.1"


def test_bind_address_comes_from_config():
    cfg = Config(host="0.0.0.0", port=9000)
    assert mcp_server.bind_address(cfg) == ("0.0.0.0", 9000)
