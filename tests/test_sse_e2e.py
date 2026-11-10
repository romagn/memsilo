"""SSE-transport session-ownership tests against a real running server.

The SSE transport cannot be driven through Starlette's TestClient: its session
only exists while a long-lived `GET /sse` stream is open, and the session id is
handed to the client in the *body* of the `endpoint` event rather than a
response header (mcp/server/sse.py:175-183). So these tests run uvicorn on an
ephemeral port and hold the GET open on a background thread, which is also the
only way to test the thing that used to be a real hole: whether ownership is
bound when the id is *minted* (during the GET) or only when the client first
POSTs — the latter leaving a window for whoever learns the id first.
"""

from __future__ import annotations

import json
import queue
import socket
import threading
import time

import httpx
import pytest
import uvicorn

import mcp_server
from users import UserStore

PROTOCOL_VERSION = "2025-06-18"
_INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": PROTOCOL_VERSION,
        "capabilities": {},
        "clientInfo": {"name": "pytest", "version": "1"},
    },
}


class _FakeMem0:
    def get_all(self, filters, top_k):
        return {"results": []}


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


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _Server:
    def __init__(self, base_url: str, registry: mcp_server.SessionRegistry):
        self.base_url = base_url
        self.registry = registry


@pytest.fixture
def server(store):
    # host="0.0.0.0" is just a test value; it disables the
    # localhost DNS-rebinding guard.
    app = mcp_server.mcp.sse_app(host="0.0.0.0")
    registry = mcp_server.SessionRegistry.from_app(app)
    app.add_middleware(mcp_server.build_auth_middleware(store, registry))

    port = _free_port()
    uv = uvicorn.Server(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=port,
            log_level="error",
            # Without this, shutdown waits out the held-open SSE stream.
            timeout_graceful_shutdown=1,
        )
    )
    thread = threading.Thread(target=uv.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not uv.started:
        if time.monotonic() > deadline:  # pragma: no cover - startup failure
            raise RuntimeError("uvicorn did not start")
        time.sleep(0.02)
    try:
        yield _Server(f"http://127.0.0.1:{port}", registry)
    finally:
        uv.should_exit = True
        thread.join(timeout=10)


class _SseStream:
    """A held-open `GET /sse`, with the lines it receives on a queue."""

    def __init__(self, base_url: str, token: str):
        self._base_url = base_url
        self._token = token
        self.lines: queue.Queue[str] = queue.Queue()
        self.status: int | None = None
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        try:
            with (
                httpx.Client(timeout=30) as client,
                client.stream(
                    "GET",
                    f"{self._base_url}/sse",
                    headers={"authorization": f"Bearer {self._token}"},
                ) as response,
            ):
                self.status = response.status_code
                self._ready.set()
                for line in response.iter_lines():
                    self.lines.put(line)
        except (httpx.HTTPError, OSError):  # pragma: no cover - the stream dies at teardown
            pass
        finally:
            self._ready.set()

    def start(self) -> _SseStream:
        self._thread.start()
        self._ready.wait(timeout=10)
        return self

    def next_line(self, timeout: float = 10) -> str:
        return self.lines.get(timeout=timeout)

    def message_endpoint(self, timeout: float = 10) -> str:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            line = self.next_line(timeout=max(0.1, deadline - time.monotonic()))
            if line.startswith("data: /messages/"):
                return line[len("data: ") :]
        raise AssertionError("no endpoint event on the SSE stream")

    def drain_for(self, seconds: float) -> list[str]:
        """Every non-blank line that arrives in the next `seconds`. Blank lines
        are SSE event terminators, not content."""
        collected: list[str] = []
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            try:
                line = self.lines.get(timeout=max(0.01, deadline - time.monotonic()))
            except queue.Empty:
                break
            if line:
                collected.append(line)
        return collected


def _session_id_of(endpoint: str) -> str:
    return endpoint.partition("session_id=")[2]


def test_sse_ownership_is_bound_when_the_id_is_minted(store, server):
    """The residual race. The session id reaches the client in the `endpoint`
    event body, so a middleware watching response *headers* could not bind it
    and had to wait for the first POST — a window in which whoever learned the
    id first could claim it. `connect_sse` records the owner against the UUID
    it mints before that event is written, so the binding already exists here,
    with no POST having been made."""
    token = store.add_user("alice", "Alice")
    stream = _SseStream(server.base_url, token).start()
    assert stream.status == 200
    endpoint = stream.message_endpoint()

    assert server.registry.owner("sse", _session_id_of(endpoint)) == "alice"


def test_sse_session_cannot_be_used_by_another_user(store, server):
    alice_token = store.add_user("alice", "Alice")
    bob_token = store.add_user("bob", "Bob")

    stream = _SseStream(server.base_url, alice_token).start()
    endpoint = stream.message_endpoint()

    hijack = httpx.post(
        server.base_url + endpoint,
        headers={"authorization": f"Bearer {bob_token}"},
        json=_INITIALIZE,
        timeout=10,
    )
    assert hijack.status_code == 403, f"{hijack.status_code} {hijack.text!r}"
    assert stream.drain_for(0.5) == [], "something reached Alice's stream despite the 403"


def test_sse_session_hijack_with_a_decoy_header_is_rejected(store, server):
    """Bypass A's shape on the SSE side: a decoy `Mcp-Session-Id` header
    alongside the victim's real `session_id` query param."""
    alice_token = store.add_user("alice", "Alice")
    bob_token = store.add_user("bob", "Bob")

    stream = _SseStream(server.base_url, alice_token).start()
    endpoint = stream.message_endpoint()

    hijack = httpx.post(
        server.base_url + endpoint,
        headers={"authorization": f"Bearer {bob_token}", "mcp-session-id": "never-issued"},
        json=_INITIALIZE,
        timeout=10,
    )
    assert hijack.status_code == 403, f"{hijack.status_code} {hijack.text!r}"


def test_sse_made_up_session_id_is_rejected(store, server):
    token = store.add_user("alice", "Alice")
    resp = httpx.post(
        f"{server.base_url}/messages/?session_id=55555555555555555555555555555555",
        headers={"authorization": f"Bearer {token}"},
        json=_INITIALIZE,
        timeout=10,
    )
    assert resp.status_code == 403, f"{resp.status_code} {resp.text!r}"


def test_sse_owner_can_use_their_own_session(store, server):
    """No false positives: Alice's own POST is accepted and its JSON-RPC
    response is delivered onto her stream."""
    token = store.add_user("alice", "Alice")
    stream = _SseStream(server.base_url, token).start()
    endpoint = stream.message_endpoint()

    accepted = httpx.post(
        server.base_url + endpoint,
        headers={"authorization": f"Bearer {token}"},
        json=_INITIALIZE,
        timeout=10,
    )
    assert accepted.status_code == 202, f"{accepted.status_code} {accepted.text!r}"

    payloads = [
        json.loads(line[len("data: ") :])
        for line in stream.drain_for(5)
        if line.startswith("data: {")
    ]
    assert any(p.get("id") == 1 and "result" in p for p in payloads), payloads


def test_sse_get_without_a_token_is_rejected(store, server):
    store.add_user("alice", "Alice")
    resp = httpx.get(f"{server.base_url}/sse", timeout=10)
    assert resp.status_code == 401
