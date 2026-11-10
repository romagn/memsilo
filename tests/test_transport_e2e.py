"""Transport-level tests against mcp_server's *real* streamable-HTTP app.

tests/test_auth.py drives the middleware over a synthetic Starlette app and
tests/test_mcp_server_e2e.py sets `current_user_id` by hand; neither speaks the
actual MCP JSON-RPC wire protocol through `mcp.streamable_http_app()`. These do.
"""

from __future__ import annotations

import json

import pytest
from starlette.testclient import TestClient

import mcp_server
from users import UserStore

PROTOCOL_VERSION = "2025-06-18"
_BASE_HEADERS = {
    "accept": "application/json, text/event-stream",
    "content-type": "application/json",
}


class _FakeMem0:
    """Same shape as tests/test_mcp_server_e2e.py's fake — no Qdrant/Anthropic."""

    def __init__(self):
        self.store: dict[str, list[dict]] = {}

    def add(self, text, user_id, metadata):
        entry = {"id": f"{user_id}-{len(self.store.get(user_id, []))}", "memory": text}
        self.store.setdefault(user_id, []).append(entry)
        return {"results": [entry]}

    def get_all(self, filters, top_k):
        return {"results": self.store.get(filters["user_id"], [])[:top_k]}

    def search(self, query, filters, top_k):
        rows = self.store.get(filters["user_id"], [])
        return {"results": [{**e, "score": 1.0} for e in rows][:top_k]}


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


@pytest.fixture
def registry_holder():
    """Lets a test reach the SessionRegistry the client fixture built."""
    return {}


@pytest.fixture
def client(store, registry_holder):
    # host="0.0.0.0" is just a test value (it disables the
    # localhost DNS-rebinding guard, which would otherwise reject "testserver").
    # Each app gets a fresh StreamableHTTPSessionManager, so session ownership
    # state is per-test by construction — no module-level map to reset.
    app = mcp_server.mcp.streamable_http_app(host="0.0.0.0")
    registry = mcp_server.SessionRegistry.from_app(app)
    registry_holder["registry"] = registry
    app.add_middleware(mcp_server.build_auth_middleware(store, registry))
    with TestClient(app) as c:
        yield c


def _headers(token: str, session_id: str | None = None) -> dict[str, str]:
    h = dict(_BASE_HEADERS)
    h["authorization"] = f"Bearer {token}"
    if session_id:
        h["mcp-session-id"] = session_id
    return h


def _sse_result(response) -> dict:
    """Pull the JSON-RPC payload out of a `text/event-stream` response body."""
    for line in response.text.splitlines():
        if line.startswith("data: "):
            return json.loads(line[len("data: ") :])
    raise AssertionError(f"no SSE data frame in response: {response.text!r}")


def _initialize(client, token: str) -> str:
    """Run `initialize` + `notifications/initialized`; return the session id."""
    resp = client.post(
        "/mcp",
        headers=_headers(token),
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "pytest", "version": "1"},
            },
        },
    )
    assert resp.status_code == 200, resp.text
    session_id = resp.headers["mcp-session-id"]
    notified = client.post(
        "/mcp",
        headers=_headers(token, session_id),
        json={"jsonrpc": "2.0", "method": "notifications/initialized"},
    )
    assert notified.status_code == 202, notified.text
    return session_id


def _call_memory_stats(client, token: str, session_id: str):
    return client.post(
        "/mcp",
        headers=_headers(token, session_id),
        json={
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "memory_stats", "arguments": {}},
        },
    )


def test_tool_call_resolves_the_token_holders_user_id(store, client):
    """Finding 5: a real JSON-RPC exchange over the real app, two tokens, each
    resolving to its own user_id."""
    alice_token = store.add_user("alice", "Alice")
    bob_token = store.add_user("bob", "Bob")

    for token, expected in ((alice_token, "alice"), (bob_token, "bob")):
        session_id = _initialize(client, token)
        resp = _call_memory_stats(client, token, session_id)
        assert resp.status_code == 200, resp.text
        payload = _sse_result(resp)
        stats = json.loads(payload["result"]["structuredContent"]["result"])
        assert stats["user_id"] == expected


def test_session_is_bound_to_the_token_that_created_it(store, client, registry_holder):
    """Finding 4: Bob holds his own valid token but targets Alice's session id.
    Before the fix this returned 200 and delivered the JSON-RPC response onto
    Alice's session; now it is rejected with 403."""
    alice_token = store.add_user("alice", "Alice")
    bob_token = store.add_user("bob", "Bob")

    alice_session = _initialize(client, alice_token)
    assert registry_holder["registry"].owner("http", alice_session) == "alice"

    hijack = _call_memory_stats(client, bob_token, alice_session)
    assert hijack.status_code == 403
    assert hijack.json() == {"error": "forbidden"}

    # Alice's own session still works afterwards.
    ok = _call_memory_stats(client, alice_token, alice_session)
    assert ok.status_code == 200, ok.text


def test_session_binding_recorded_when_the_session_id_is_minted(store, client):
    """The streamable-HTTP session id is minted while `initialize` is being
    handled and returned in its response, so ownership must be bound at that
    moment — not on the client's next request, which would leave a race in
    which another token holder claims the session first. `mcp`'s
    StreamableHTTPSessionManager records the owner inside the same
    `_session_creation_lock` block that mints the id, so there is no window."""
    alice_token = store.add_user("alice", "Alice")
    bob_token = store.add_user("bob", "Bob")

    resp = client.post(
        "/mcp",
        headers=_headers(alice_token),
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "pytest", "version": "1"},
            },
        },
    )
    session_id = resp.headers["mcp-session-id"]
    # Bob races in before Alice sends notifications/initialized.
    stolen = client.post(
        "/mcp",
        headers=_headers(bob_token, session_id),
        json={"jsonrpc": "2.0", "method": "notifications/initialized"},
    )
    assert stolen.status_code == 403


def test_unauthenticated_request_to_real_app_rejected(store, client):
    store.add_user("alice", "Alice")
    resp = client.post(
        "/mcp",
        headers=_BASE_HEADERS,
        json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
    )
    assert resp.status_code == 401
