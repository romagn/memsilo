from uuid import UUID

import pytest
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from mcp_server import SessionRegistry, current_user_id, resolve_user_id
from users import UserStore


def _issued(owners_by_session):
    """A registry of sessions the server has 'issued', in the shape the real
    mcp transports keep them: session_id -> AuthorizationContext. The real
    `SessionRegistry.from_app` reads exactly these maps off the live
    transports; here they are supplied directly so the middleware's own gate
    can be exercised over a synthetic app that has no MCP transport at all.
    """
    return {
        sid: {"client_id": uid, "issuer": None, "subject": None}
        for sid, uid in owners_by_session.items()
    }


def _build_test_app(store: UserStore, registry: SessionRegistry | None = None):
    from mcp_server import build_auth_middleware

    async def whoami(request):
        return PlainTextResponse(resolve_user_id())

    async def health(request):
        return PlainTextResponse("ok")

    app = Starlette(
        routes=[
            Route("/whoami", whoami),
            Route("/health", health),
        ]
    )
    app.add_middleware(build_auth_middleware(store, registry or SessionRegistry()))
    return app


@pytest.fixture
def store(tmp_path):
    return UserStore(str(tmp_path / "users.json"))


def test_valid_token_resolves_user(store):
    token = store.add_user("alice", "Alice")
    client = TestClient(_build_test_app(store))
    resp = client.get("/whoami", headers={"authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    assert resp.text == "alice"


def test_missing_token_rejected(store):
    store.add_user("alice", "Alice")
    client = TestClient(_build_test_app(store))
    resp = client.get("/whoami")
    assert resp.status_code == 401


def test_invalid_token_rejected(store):
    store.add_user("alice", "Alice")
    client = TestClient(_build_test_app(store))
    resp = client.get("/whoami", headers={"authorization": "Bearer garbage"})
    assert resp.status_code == 401


def test_revoked_token_rejected(store):
    token = store.add_user("alice", "Alice")
    store.revoke_user("alice")
    client = TestClient(_build_test_app(store))
    resp = client.get("/whoami", headers={"authorization": f"Bearer {token}"})
    assert resp.status_code == 401


def test_health_open_without_token(store):
    client = TestClient(_build_test_app(store))
    resp = client.get("/health")
    assert resp.status_code == 200


def test_two_users_get_different_context(store):
    token_a = store.add_user("alice", "Alice")
    token_b = store.add_user("bob", "Bob")
    client = TestClient(_build_test_app(store))
    resp_a = client.get("/whoami", headers={"authorization": f"Bearer {token_a}"})
    resp_b = client.get("/whoami", headers={"authorization": f"Bearer {token_b}"})
    assert resp_a.text == "alice"
    assert resp_b.text == "bob"


def test_resolve_user_id_defaults_outside_request(store):
    # No request context set (e.g. stdio path) — falls back to config default.
    assert current_user_id.get(None) is None
    assert resolve_user_id() == "default"


def test_resolve_user_id_does_not_fall_back_on_empty_string():
    """A falsy *string* means a request DID authenticate. Falling back to
    config.default_user_id there would silently escalate the caller to the
    configured default user; only genuine absence (None) may fall back."""
    reset = current_user_id.set("")
    try:
        assert resolve_user_id() == ""
        assert resolve_user_id() != "default"
    finally:
        current_user_id.reset(reset)


@pytest.mark.parametrize("scheme", ["Bearer", "bearer", "BEARER", "BeArEr"])
def test_bearer_scheme_is_case_insensitive(store, scheme):
    # RFC 7235: the auth scheme is case-insensitive.
    token = store.add_user("alice", "Alice")
    client = TestClient(_build_test_app(store))
    resp = client.get("/whoami", headers={"authorization": f"{scheme} {token}"})
    assert resp.status_code == 200
    assert resp.text == "alice"


def test_non_bearer_scheme_still_rejected(store):
    token = store.add_user("alice", "Alice")
    client = TestClient(_build_test_app(store))
    resp = client.get("/whoami", headers={"authorization": f"Basic {token}"})
    assert resp.status_code == 401


def test_session_registry_from_app_refuses_an_app_with_no_transport():
    """Fail loudly, not quietly. An empty registry would reject every
    legitimate session request; silently skipping the check would be the
    regression this whole guard exists to prevent. If a future `mcp` release
    moves the transports, `from_app` raises here — and every transport e2e
    test builds its registry this way, so CI catches it."""
    with pytest.raises(RuntimeError, match="No mcp SSE or streamable-HTTP transport"):
        SessionRegistry.from_app(Starlette(routes=[]))


def test_session_registry_is_backed_by_the_live_transport_maps():
    """`from_app` must hand back the transports' own dicts, not a copy — a
    copy would go stale the moment a session was created or ended."""
    import mcp_server

    app = mcp_server.mcp.streamable_http_app(host="0.0.0.0")
    registry = SessionRegistry.from_app(app)
    manager = next(
        route.app.session_manager
        for route in app.routes
        if hasattr(getattr(route, "app", None), "session_manager")
    )
    manager._session_owners["live"] = {"client_id": "alice", "issuer": None, "subject": None}
    assert registry.owner("http", "live") == "alice"
    del manager._session_owners["live"]
    assert registry.owner("http", "live") is None


ALICE_SSE_SESSION = UUID("11111111-1111-4111-8111-111111111111")
ALICE_HTTP_SESSION = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"


@pytest.fixture
def registry():
    """Alice owns one session on each transport; nobody owns anything else."""
    return SessionRegistry(
        sse_owners=_issued({ALICE_SSE_SESSION: "alice"}),
        http_owners=_issued({ALICE_HTTP_SESSION: "alice"}),
    )


def test_sse_session_usable_by_its_own_owner(store, registry):
    token = store.add_user("alice", "Alice")
    client = TestClient(_build_test_app(store, registry))
    resp = client.get(
        f"/whoami?session_id={ALICE_SSE_SESSION.hex}", headers={"authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 200
    assert resp.text == "alice"


def test_sse_session_rejected_for_another_user(store, registry):
    store.add_user("alice", "Alice")
    token_b = store.add_user("bob", "Bob")
    client = TestClient(_build_test_app(store, registry))
    resp = client.get(
        f"/whoami?session_id={ALICE_SSE_SESSION.hex}",
        headers={"authorization": f"Bearer {token_b}"},
    )
    assert resp.status_code == 403


def test_http_session_rejected_for_another_user(store, registry):
    store.add_user("alice", "Alice")
    token_b = store.add_user("bob", "Bob")
    client = TestClient(_build_test_app(store, registry))
    resp = client.get(
        "/whoami",
        headers={"authorization": f"Bearer {token_b}", "mcp-session-id": ALICE_HTTP_SESSION},
    )
    assert resp.status_code == 403


def test_session_identifier_the_server_never_issued_is_rejected(store, registry):
    """Not silently recorded as a new binding — rejected. This is what makes
    the ownership map un-writable by an attacker, and so un-floodable."""
    token = store.add_user("alice", "Alice")
    client = TestClient(_build_test_app(store, registry))
    made_up = client.get(
        "/whoami?session_id=22222222222222222222222222222222",
        headers={"authorization": f"Bearer {token}"},
    )
    assert made_up.status_code == 403
    # ...and naming it did not create a binding for the caller either.
    assert registry.owner("sse", "22222222222222222222222222222222") is None


def test_every_session_identifier_on_a_request_is_checked(store, registry):
    """A decoy identifier for the transport that is *not* routing this request
    must not launder it. Both orders are covered: the guard reads every
    identifier present, not the first one it happens to find."""
    store.add_user("alice", "Alice")
    token_b = store.add_user("bob", "Bob")
    client = TestClient(_build_test_app(store, registry))

    # Bob: unclaimed query param (would have passed the old first-match check)
    # + Alice's real session header.
    decoy_first = client.get(
        "/whoami?session_id=33333333333333333333333333333333",
        headers={"authorization": f"Bearer {token_b}", "mcp-session-id": ALICE_HTTP_SESSION},
    )
    assert decoy_first.status_code == 403

    # The mirror image: Alice's real SSE session id plus a decoy header.
    decoy_second = client.get(
        f"/whoami?session_id={ALICE_SSE_SESSION.hex}",
        headers={"authorization": f"Bearer {token_b}", "mcp-session-id": "never-issued"},
    )
    assert decoy_second.status_code == 403


def test_repeated_session_id_query_params_are_all_checked(store, registry):
    token = store.add_user("alice", "Alice")
    client = TestClient(_build_test_app(store, registry))
    resp = client.get(
        f"/whoami?session_id={ALICE_SSE_SESSION.hex}&session_id=44444444444444444444444444444444",
        headers={"authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 403
