"""memsilo — multi-user memory MCP server.

Exposes memory (Mem0 + Qdrant) as MCP tools and resources. Runs
standalone:

    python mcp_server.py

Remote transports (SSE at MEMSILO_TRANSPORT=sse, Streamable HTTP at
MEMSILO_TRANSPORT=streamable-http) require a per-user Bearer token — see
scripts/manage_users.py. stdio (the default, for an MCP client that launches the
server as a local process) is unauthenticated and acts as the configured default
user, MEMSILO_DEFAULT_USER_ID: whoever can launch a local process is already
trusted with the files it reads.
"""

from __future__ import annotations

import contextvars
import json
import logging
import os
import sys
from urllib.parse import parse_qsl
from uuid import UUID

sys.path.insert(0, os.path.dirname(__file__))

# The installed `mcp` package is 2.x, where `FastMCP` was renamed to
# `MCPServer` (mcp.server.fastmcp is a stub that raises ModuleNotFoundError
# with a pointer to the migration guide). MCPServer keeps the same
# decorator-based API (`.tool()`, `.resource()`, `.run()`, `.sse_app()`,
# `.streamable_http_app()`), so only the import/construction line changes.
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.auth.provider import AccessToken
from mcp.server.mcpserver import MCPServer
from mcp.server.sse import SseServerTransport
from mcp.server.streamable_http_manager import StreamableHTTPASGIApp, StreamableHTTPSessionManager

import memory
from config import config

# Mem0 reads MEM0_TELEMETRY once, when it is first imported (lazily, in
# memory.build_memory), and importing mem0 imports onnxruntime, which reads
# ORT_DISABLE_TELEMETRY at that moment: set them before anything imports mem0.
os.environ.update(memory.telemetry_env(config.telemetry))

from memory import build_memory
from users import UserStore

mcp = MCPServer("memsilo")

_user_store = UserStore(config.users_file)

# Set by the auth middleware for the duration of one SSE/streamable-HTTP
# request; left unset on stdio, where resolve_user_id() falls back to
# config.default_user_id.
current_user_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "current_user_id", default=None
)


def resolve_user_id() -> str:
    # Fall back to the configured default user ONLY on genuine absence — i.e.
    # the stdio path, where the middleware never runs. A contextvar holding a
    # falsy *string* means a request did authenticate, and must never silently
    # resolve to the default user_id.
    uid = current_user_id.get()
    return uid if uid is not None else config.default_user_id


def _bearer_token(auth_header: str) -> str:
    """Extract the token from an Authorization header. RFC 7235 makes the auth
    scheme case-insensitive, so `bearer`/`BEARER`/`Bearer` all work."""
    scheme, _, token = auth_header.partition(" ")
    return token.strip() if scheme.lower() == "bearer" else ""


def _principal(user_id: str) -> AuthenticatedUser:
    """The `scope["user"]` an authenticated request carries.

    `mcp` 2.1.1 enforces session ownership *itself*, at the transport layer,
    keyed on this object: `SseServerTransport.connect_sse` records
    `authorization_context(scope["user"])` against the session UUID it mints
    (sse.py:155-159) and `_handle_post_message` refuses a POST whose principal
    differs (sse.py:259-266); `StreamableHTTPSessionManager` does the same for
    the session id it mints in `_handle_stateful_request`
    (streamable_http_manager.py:253-299). Both comparisons run on the
    *identifier the transport actually routes by*, at the moment it is minted
    and again at the moment it is used — which is why populating this is the
    authoritative half of the fix, not a convenience.

    `principal_components()` reduces the token to (client_id, issuer, subject);
    with only `client_id` set, the comparison is exactly a user_id comparison.
    The raw bearer token is deliberately NOT carried here — nothing downstream
    needs it.
    """
    return AuthenticatedUser(AccessToken(token="", client_id=user_id, scopes=[]))


class SessionRegistry:
    """Read-only view of the sessions the *server itself* issued, and to whom.

    Both maps belong to the live `mcp` transport objects, so an entry exists
    only for an identifier the server minted, appears the instant it is minted,
    and disappears when the session ends. Keeping no copy of our own is the
    point: the previous middleware-level map was writable by any authenticated
    request that merely *named* an identifier, and evicted oldest-first at a
    fixed cap — so a flood of invented identifiers evicted a victim's real
    binding and re-opened a hijack that had been blocked.
    """

    def __init__(self, sse_owners=None, http_owners=None) -> None:
        self._sse_owners = sse_owners
        self._http_owners = http_owners

    @classmethod
    def from_app(cls, app):
        """Locate the transport objects inside a `mcp.sse_app()` /
        `mcp.streamable_http_app()` Starlette app.

        `sse_app()` mounts the message endpoint as `sse.handle_post_message`
        (a bound method, so `__self__` is the transport) and
        `streamable_http_app()` routes to a `StreamableHTTPASGIApp` holding
        the session manager. Raises rather than degrading quietly: an empty
        registry would reject all legitimate session traffic, and a silently
        skipped check would be exactly the regression this guards against.
        """
        sse_owners = http_owners = None
        for route in getattr(app, "routes", []):
            for candidate in (getattr(route, "app", None), getattr(route, "endpoint", None)):
                bound_to = getattr(candidate, "__self__", None)
                if isinstance(bound_to, SseServerTransport):
                    sse_owners = cls._owner_map(bound_to)
                if isinstance(candidate, StreamableHTTPASGIApp):
                    manager = candidate.session_manager
                    if isinstance(manager, StreamableHTTPSessionManager):
                        http_owners = cls._owner_map(manager)
        if sse_owners is None and http_owners is None:
            raise RuntimeError(
                "No mcp SSE or streamable-HTTP transport found on the app — "
                "cannot validate session ownership. This means the installed "
                "mcp package no longer exposes the transports the way "
                "SessionRegistry.from_app expects (verified against mcp 2.1.1)."
            )
        return cls(sse_owners, http_owners)

    @staticmethod
    def _owner_map(transport):
        """`transport._session_owners`, insisting it is there. Both mcp
        transports keep one (verified against mcp 2.1.1); if a future release
        renames or drops it, say so at startup rather than silently handing
        back an empty registry that rejects every real session."""
        owners = getattr(transport, "_session_owners", None)
        if owners is None:
            raise RuntimeError(
                f"{type(transport).__name__} has no _session_owners map — the installed "
                "mcp package no longer tracks session ownership where "
                "SessionRegistry expects it (verified against mcp 2.1.1)."
            )
        return owners

    def owner(self, scheme: str, session_id: str) -> str | None:
        """The user_id the server issued `session_id` to, or None if it never
        issued it (or the session has since ended).

        `scheme` says which identifier this is — "sse" for the `session_id`
        query param, "http" for the `Mcp-Session-Id` header. They are separate
        namespaces: `SseServerTransport` keys its map by UUID, the streamable-
        HTTP manager by the id's hex string, and an id from one transport must
        never satisfy a check against the other.
        """
        if scheme == "sse":
            owners = self._sse_owners
            try:
                key: object = UUID(hex=session_id)
            except ValueError:
                return None  # not even shaped like an id this server mints
        else:
            owners = self._http_owners
            key = session_id
        if owners is None:
            return None  # this app serves no such transport — nothing issued
        context = owners.get(key)
        return context.get("client_id") if context else None


def _session_identifiers(scope) -> list[tuple[str, str]]:
    """EVERY session identifier the request carries, not just the first one
    found.

    Two transports name a session two ways: SSE POSTs to `/messages/` carry a
    `session_id` query param, streamable HTTP carries an `Mcp-Session-Id`
    header. Checking only the first found is what made the original guard
    bypassable — a request could carry an unclaimed query param (which passed
    the check) *alongside* a victim's real session header (which the transport
    then routed the response by). Repeated occurrences of either are returned
    too, so a legitimate-looking value cannot mask an illegitimate one.
    """
    found: list[tuple[str, str]] = []
    query_string = scope.get("query_string", b"").decode("latin-1")
    for name, value in parse_qsl(query_string, keep_blank_values=False):
        if name == "session_id" and value:
            found.append(("sse", value))
    for raw_name, raw_value in scope.get("headers", []):
        if raw_name.lower() == b"mcp-session-id":
            value = raw_value.decode("latin-1").strip()
            if value:
                found.append(("http", value))
    return found


def build_auth_middleware(store: UserStore, session_registry: SessionRegistry):
    """Return ASGI middleware that resolves a Bearer token to a user_id via
    `store` and sets `current_user_id` for the request. `/health` is exempt.

    401s on a missing, unknown, or revoked token. 403s when the request names
    ANY session identifier that `session_registry` says the server did not
    issue to this user — including one it never issued at all, which cannot be
    a legitimate session request and so is rejected rather than recorded.

    It also sets `scope["user"]`, which switches on `mcp`'s own transport-level
    ownership enforcement (see `_principal`). That is the layer that actually
    binds a session at the moment the id is minted and checks it at the moment
    the transport routes by it; the middleware check above is a second,
    independent gate over the same server-issued registry.

    Plain ASGI rather than BaseHTTPMiddleware: `scope` has to be mutated in
    place for the transports beneath to see the principal, and the response no
    longer needs inspecting now that no binding is recorded from it.
    """
    from starlette.authentication import AuthCredentials
    from starlette.responses import JSONResponse

    class AuthMiddleware:
        def __init__(self, app) -> None:
            self.app = app

        async def __call__(self, scope, receive, send):
            if scope["type"] != "http" or scope.get("path") == "/health":
                return await self.app(scope, receive, send)

            headers = {k.lower(): v for k, v in scope.get("headers", [])}
            token = _bearer_token(headers.get(b"authorization", b"").decode("latin-1"))
            user_id = store.resolve(token) if token else None
            if user_id is None:
                response = JSONResponse({"error": "unauthorized"}, status_code=401)
                return await response(scope, receive, send)

            for identifier_scheme, session_id in _session_identifiers(scope):
                if session_registry.owner(identifier_scheme, session_id) != user_id:
                    response = JSONResponse({"error": "forbidden"}, status_code=403)
                    return await response(scope, receive, send)

            scope["user"] = _principal(user_id)
            scope["auth"] = AuthCredentials([])

            reset_token = current_user_id.set(user_id)
            try:
                await self.app(scope, receive, send)
            finally:
                current_user_id.reset(reset_token)

    return AuthMiddleware


_memory = None


def _mem0():
    global _memory
    if _memory is None:
        _memory = build_memory(config)
    return _memory


MAX_RESULTS = 200

# Server-side only. A backend exception can carry internal URLs (http://qdrant:6333),
# response bodies or other users' data, so its text is logged here and never
# returned to the token holder; tools answer with a fixed message instead.
logger = logging.getLogger("memsilo.tools")
INTERNAL_ERROR = "internal error"


def _internal_error(tool: str, **shape) -> str:
    """Log the exception being handled and return `tool`'s generic error JSON."""
    logger.exception("%s failed", tool)
    return json.dumps({**shape, "error": INTERNAL_ERROR})


_RESERVED_METADATA_KEYS = frozenset({"user_id", "agent_id", "run_id", "actor_id"})

_NOT_FOUND = {"deleted": False, "error": "not found or not owned by you"}
_CLEANUP_FAILED = (
    "the memory was deleted, but cleanup of its stored history failed; "
    "the operator has the details in the server log"
)
NOTHING_STORED = (
    "no facts were extracted; nothing was stored. If the server uses a reasoning "
    "(thinking) model, disable reasoning: it can use up the reply and leave it empty."
)


def _is_memory_id(value: str) -> bool:
    """True only for a canonical (lower-case, hyphenated) UUID, the form of
    the ids Mem0 mints (str(uuid.uuid4()))."""
    try:
        return str(UUID(value)) == value
    except (TypeError, ValueError, AttributeError):
        return False


def _clamp_limit(limit: int) -> int:
    """Clamp a caller-supplied result limit to 1..MAX_RESULTS."""
    return max(1, min(int(limit), MAX_RESULTS))


def bind_address(cfg) -> tuple[str, int]:
    """The (host, port) the remote transports listen on, from config."""
    return cfg.host, cfg.port


@mcp.tool()
def search_memory(query: str, limit: int = 10) -> str:
    """Search your memories by semantic similarity.

    Use this to find what's remembered about a topic, person, project,
    preference, or past decision. Returns the most relevant memories, scoped
    to your own user account.
    """
    user_id = resolve_user_id()
    limit = _clamp_limit(limit)
    try:
        results = _mem0().search(query, filters={"user_id": user_id}, top_k=limit)
    except Exception:  # noqa: BLE001 -- never return backend error text
        return _internal_error("search_memory")
    if isinstance(results, dict):
        results = results.get("results", [])
    memories = []
    for r in results:
        mem = r.get("memory", str(r))
        # `.get("score", 0)` only defaults a *missing* key — mem0 returning an
        # explicit None would reach round() and raise TypeError.
        score = r.get("score") or 0
        memories.append({"memory": mem, "score": round(score, 3)})
    return json.dumps(memories, indent=2)


@mcp.tool()
def add_memory(text: str, metadata: str = "{}") -> str:
    """Store a new memory. Mem0 extracts facts from the text with an LLM and
    stores them. (In the tested Mem0 version, 2.0.20, it adds new facts and does
    not replace older facts that contradict them.)

    Use this to remember a preference, decision, project context, or any
    fact worth keeping across sessions. Stored under your own user account.
    If no fact was extracted, nothing is stored and the reply carries a
    "warning".
    """
    try:
        meta = json.loads(metadata) if metadata != "{}" else {}
    except json.JSONDecodeError as e:
        return json.dumps({"stored": 0, "error": f"invalid metadata JSON: {e}"})
    if not isinstance(meta, dict):
        return json.dumps({"stored": 0, "error": "metadata must be a JSON object"})
    for key in meta:
        if str(key).strip().lower() in _RESERVED_METADATA_KEYS:
            return json.dumps(
                {"stored": 0, "error": f"metadata key {str(key).strip().lower()!r} is reserved"}
            )
    user_id = resolve_user_id()
    try:
        result = _mem0().add(text, user_id=user_id, metadata=meta)
    except Exception:  # noqa: BLE001 -- never return backend error text
        return _internal_error("add_memory", stored=0)
    results = result.get("results", []) if isinstance(result, dict) else []
    reply: dict = {"stored": len(results), "results": results}
    if not results:
        # Mem0 turns an empty LLM reply into "no facts" without an error, so
        # the text would be lost silently. Never log the text itself.
        logger.warning("add_memory: no facts extracted, nothing stored (%d chars)", len(text))
        reply["warning"] = NOTHING_STORED
    return json.dumps(reply, indent=2, default=str)


@mcp.tool()
def get_all_memories(limit: int = 50) -> str:
    """List all of your stored memories. Returns up to `limit` memories."""
    user_id = resolve_user_id()
    limit = _clamp_limit(limit)
    # mem0ai's Memory.get_all() defaults top_k to 20 — silently truncating
    # results far below `limit` with no error or warning. Pass it explicitly
    # so `limit` (the parameter this function actually promises) governs the
    # result.
    try:
        results = _mem0().get_all(filters={"user_id": user_id}, top_k=limit)
    except Exception:  # noqa: BLE001 -- never return backend error text
        return _internal_error("get_all_memories")
    if isinstance(results, dict):
        results = results.get("results", [])
    memories = []
    for r in results[:limit]:
        memories.append(
            {
                "id": r.get("id", ""),
                "memory": r.get("memory", str(r)),
                "created_at": r.get("created_at", ""),
            }
        )
    return json.dumps(memories, indent=2, default=str)


@mcp.tool()
def delete_memory(memory_id: str) -> str:
    """Delete one of your own memories by ID. Refuses to delete a memory
    that belongs to a different user, even if you know its ID."""
    user_id = resolve_user_id()
    if not _is_memory_id(memory_id):
        # Not an id Mem0 could have minted: answer as for any unknown id rather
        # than let Qdrant reject it (a 400, logged as a traceback).
        return json.dumps(_NOT_FOUND)
    try:
        existing = _mem0().get(memory_id)
    except Exception:  # noqa: BLE001 -- tool boundary: generic JSON error, never backend text
        return _internal_error("delete_memory", deleted=False)
    if not existing or existing.get("user_id") != user_id:
        return json.dumps(_NOT_FOUND)
    try:
        _mem0().delete(memory_id)
    except Exception:  # noqa: BLE001 -- tool boundary: generic JSON error, never backend text
        return _internal_error("delete_memory", deleted=False)
    # Mem0's delete leaves this memory's ADD/UPDATE/DELETE history rows, full
    # text included, and a later erase only finds ids of live memories: purge
    # them now, while the id is known and its owner has just been checked.
    try:
        memory.purge_history(config.history_db, [memory_id])
        left = memory.remaining_rows(config.history_db, [memory_id], user_id)[0]
    except Exception:
        logger.exception("delete_memory: history purge failed for a deleted memory")
        return json.dumps({"deleted": True, "id": memory_id, "error": _CLEANUP_FAILED})
    if left:
        logger.error("delete_memory: %d history rows left after purge", left)
        return json.dumps({"deleted": True, "id": memory_id, "error": _CLEANUP_FAILED})
    return json.dumps({"deleted": True, "id": memory_id})


@mcp.tool()
def memory_stats() -> str:
    """Get memory statistics — total count and a sample of recent memories."""
    user_id = resolve_user_id()
    try:
        results = _mem0().get_all(filters={"user_id": user_id}, top_k=100_000)
    except Exception:  # noqa: BLE001 -- never return backend error text
        return _internal_error("memory_stats")
    if isinstance(results, dict):
        results = results.get("results", [])
    recent = [r.get("memory", "")[:80] for r in results[-5:]]
    return json.dumps(
        {
            "total_memories": len(results),
            "user_id": user_id,
            "recent_sample": recent,
        },
        indent=2,
    )


@mcp.resource("memory://stats")
def resource_stats() -> str:
    """Memory system statistics."""
    return memory_stats()


@mcp.resource("memory://all")
def resource_all_memories() -> str:
    """All of your memories."""
    return get_all_memories(limit=100)


@mcp.custom_route("/health", methods=["GET"])
async def health_check(request):
    """Unauthenticated liveness probe — exempted in build_auth_middleware."""
    from starlette.responses import PlainTextResponse

    return PlainTextResponse("ok")


if __name__ == "__main__":
    # Fail at startup, for every transport, rather than at the first add_memory.
    config.require_llm_key()
    from qdrant_client import QdrantClient

    from memory import verify_collection_dims

    verify_collection_dims(
        QdrantClient(url=config.qdrant_url, api_key=config.qdrant_api_key or None),
        config.collection,
        config.embedder_dims,
    )
    transport = config.transport
    if transport in ("sse", "streamable-http"):
        # mcp 2.x's MCPServer.run(transport=...) builds its Starlette app
        # internally (a fresh instance per call, via self.sse_app()/
        # self.streamable_http_app()) and serves it with uvicorn — there is
        # no `middleware=` kwarg or `mcp.settings` hook to inject our own
        # Starlette middleware into that internal app. So for the remote
        # transports we build the app ourselves (same sse_app()/
        # streamable_http_app() methods `.run()` would use internally),
        # attach the auth middleware the same way tests/test_auth.py does
        # (`app.add_middleware(...)`), and serve it with uvicorn directly —
        # bypassing mcp.run() only for these two branches. stdio (the
        # `else` branch below) is untouched.
        import uvicorn

        host, port = bind_address(config)
        if transport == "sse":
            app = mcp.sse_app(host=host)
        else:
            app = mcp.streamable_http_app(host=host)
        # Built from the app so the middleware validates session identifiers
        # against the transports' own registries of server-issued sessions.
        app.add_middleware(build_auth_middleware(_user_store, SessionRegistry.from_app(app)))
        uvicorn.run(app, host=host, port=port, log_level=mcp.settings.log_level.lower())
    else:
        mcp.run()
