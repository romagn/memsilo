"""Session-hijack regression tests for the two confirmed bypasses of the
session-ownership guard, driven through mcp_server's *real* streamable-HTTP
app (tests/test_transport_e2e.py covers the happy paths; these cover the
attack shapes).

The threat is not cross-user memory reads — tool-level isolation is enforced
separately in resolve_user_id()/delete_memory(). It is *response injection*:
one authenticated user getting the JSON-RPC response to their own
tool call delivered onto a different user's session stream.

Bypass A — the guard read only the FIRST session identifier it found (the
`session_id` query param, checked before the `Mcp-Session-Id` header). A
request carrying a bogus query param *and* the victim's real header passed
the guard (the query param was unclaimed) while the transport routed the
response by the header.

Bypass B — the guard's owner map was writable by any authenticated request
that merely *mentioned* a session identifier, and was evicted oldest-first at
a fixed cap. Flooding it with made-up identifiers evicted the victim's real
binding, re-opening a previously-blocked hijack.
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
def client(store):
    app = mcp_server.mcp.streamable_http_app(host="0.0.0.0")
    app.add_middleware(
        mcp_server.build_auth_middleware(store, mcp_server.SessionRegistry.from_app(app))
    )
    with TestClient(app) as c:
        yield c


def _headers(token: str, session_id: str | None = None, **extra: str) -> dict[str, str]:
    h = dict(_BASE_HEADERS)
    h["authorization"] = f"Bearer {token}"
    if session_id:
        h["mcp-session-id"] = session_id
    h.update(extra)
    return h


def _initialize(client, token: str) -> str:
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


def _stats_call(client, token: str, url: str = "/mcp", **header_kwargs):
    return client.post(
        url,
        headers=_headers(token, **header_kwargs),
        json={
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "memory_stats", "arguments": {}},
        },
    )


def test_bypass_a_decoy_query_param_alongside_victim_session_header(store, client):
    """Bypass A. Bob sends BOTH an unclaimed `?session_id=` query param and
    Alice's real `Mcp-Session-Id` header. The old guard checked only the query
    param (unclaimed -> allowed, recorded as Bob's) while the transport routed
    the response by the header — landing Bob's tool output on Alice's stream.
    """
    alice_token = store.add_user("alice", "Alice")
    bob_token = store.add_user("bob", "Bob")

    alice_session = _initialize(client, alice_token)

    hijack = _stats_call(
        client,
        bob_token,
        url="/mcp?session_id=decoy-never-issued",
        session_id=alice_session,
    )
    assert hijack.status_code == 403, (
        f"decoy query param bypassed the session guard: {hijack.status_code} {hijack.text!r}"
    )

    # Alice's session is untouched and still hers.
    ok = _stats_call(client, alice_token, session_id=alice_session)
    assert ok.status_code == 200, ok.text


def test_bypass_a_reversed_decoy_session_header_alongside_query_param(store, client):
    """The mirror image: a decoy `Mcp-Session-Id` header must not be able to
    launder a request that also names someone else's session another way."""
    alice_token = store.add_user("alice", "Alice")
    bob_token = store.add_user("bob", "Bob")

    alice_session = _initialize(client, alice_token)

    hijack = _stats_call(
        client,
        bob_token,
        url=f"/mcp?session_id={alice_session}",
        session_id="decoy-never-issued",
    )
    assert hijack.status_code == 403, f"{hijack.status_code} {hijack.text!r}"


def test_bypass_a_repeated_query_params_are_all_validated(store, client):
    """A repeated `session_id` query param must not let one legitimate-looking
    value mask a second, illegitimate one."""
    alice_token = store.add_user("alice", "Alice")
    bob_token = store.add_user("bob", "Bob")

    alice_session = _initialize(client, alice_token)

    hijack = _stats_call(
        client,
        bob_token,
        url="/mcp?session_id=decoy-a&session_id=decoy-b",
        session_id=alice_session,
    )
    assert hijack.status_code == 403, f"{hijack.status_code} {hijack.text!r}"


def test_bypass_b_flooding_cannot_evict_a_real_binding(store, client, monkeypatch):
    """Bypass B. The owner map was writable by any authenticated request that
    merely *named* a session identifier, and evicted oldest-first at a fixed
    cap — so a flood of made-up identifiers evicted the victim's real binding
    and re-opened a previously-blocked hijack. The cap is lowered here so the
    flood is cheap; at the shipped cap it was ~10,000 requests.
    """
    # raising=False: after the fix the module has no such cap at all, because
    # it keeps no attacker-writable map to bound.
    monkeypatch.setattr(mcp_server, "_SESSION_OWNERS_MAX", 3, raising=False)

    alice_token = store.add_user("alice", "Alice")
    bob_token = store.add_user("bob", "Bob")

    alice_session = _initialize(client, alice_token)

    # The flood's own responses are irrelevant — the old guard recorded a
    # binding *before* calling downstream, so the map was polluted whatever
    # the transport then answered. What matters is the hijack afterwards.
    for i in range(25):
        _stats_call(client, bob_token, url=f"/mcp?session_id=junk-{i}")

    hijack = _stats_call(client, bob_token, session_id=alice_session)
    assert hijack.status_code == 403, (
        f"flood-and-evict re-opened the hijack: {hijack.status_code} {hijack.text!r}"
    )

    ok = _stats_call(client, alice_token, session_id=alice_session)
    assert ok.status_code == 200, ok.text


def test_session_identifier_the_server_never_issued_is_rejected(store, client):
    """A request naming an identifier the server never minted cannot be a
    legitimate session request — reject it rather than silently recording it
    as a new binding."""
    token = store.add_user("alice", "Alice")
    resp = _stats_call(client, token, session_id="0123456789abcdef0123456789abcdef")
    assert resp.status_code == 403, f"{resp.status_code} {resp.text!r}"


def test_legitimate_session_traffic_is_unaffected(store, client):
    """No false positives: a session the server itself issued to a user keeps
    working for that user, across several requests, and resolves to their own
    user_id."""
    alice_token = store.add_user("alice", "Alice")
    bob_token = store.add_user("bob", "Bob")

    for token, expected in ((alice_token, "alice"), (bob_token, "bob")):
        session_id = _initialize(client, token)
        for _ in range(3):
            resp = _stats_call(client, token, session_id=session_id)
            assert resp.status_code == 200, resp.text
            payload = next(
                json.loads(line[len("data: ") :])
                for line in resp.text.splitlines()
                if line.startswith("data: ")
            )
            stats = json.loads(payload["result"]["structuredContent"]["result"])
            assert stats["user_id"] == expected
