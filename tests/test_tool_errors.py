"""A failing backend must never leak its exception text to a token holder.

Qdrant and Mem0 error messages can carry internal URLs (http://qdrant:6333),
response bodies and other users' data. Tools log the exception server-side and
return a fixed message in their usual shape.
"""

from __future__ import annotations

import json
import logging

import pytest

import mcp_server

SECRET = "secret http://qdrant:6333 body"
_ID = "3f2b8c1e-7a4d-4e5f-9b6a-1c2d3e4f5a6b"  # a well-formed Mem0 id


class _Exploding:
    def __getattr__(self, name):
        def boom(*args, **kwargs):
            raise RuntimeError(SECRET)

        return boom


@pytest.fixture
def exploding(monkeypatch):
    fake = _Exploding()
    monkeypatch.setattr(mcp_server, "_mem0", lambda: fake)
    tok = mcp_server.current_user_id.set("alice")
    yield fake
    mcp_server.current_user_id.reset(tok)


CASES = [
    ("search_memory", {"query": "q"}, {"error": "internal error"}),
    ("add_memory", {"text": "t"}, {"stored": 0, "error": "internal error"}),
    ("get_all_memories", {}, {"error": "internal error"}),
    ("delete_memory", {"memory_id": _ID}, {"deleted": False, "error": "internal error"}),
    ("memory_stats", {}, {"error": "internal error"}),
]


@pytest.mark.parametrize(("tool", "args", "expected"), CASES)
def test_tool_returns_generic_error_and_logs(exploding, caplog, tool, args, expected):
    with caplog.at_level(logging.ERROR, logger="memsilo.tools"):
        out = getattr(mcp_server, tool)(**args)
    assert "qdrant" not in out and "secret" not in out
    assert json.loads(out) == expected
    assert any(SECRET in (r.exc_text or "") or SECRET in r.getMessage() for r in caplog.records)
    assert any(r.exc_info for r in caplog.records)


def test_delete_memory_failing_after_ownership_check_is_generic(monkeypatch, caplog):
    class _DeleteFails:
        def get(self, memory_id):
            return {"id": memory_id, "user_id": "alice"}

        def delete(self, memory_id):
            raise RuntimeError(SECRET)

    monkeypatch.setattr(mcp_server, "_mem0", lambda: _DeleteFails())
    tok = mcp_server.current_user_id.set("alice")
    try:
        out = mcp_server.delete_memory(_ID)
    finally:
        mcp_server.current_user_id.reset(tok)
    assert json.loads(out) == {"deleted": False, "error": "internal error"}


@pytest.mark.parametrize(("tool", "args", "expected"), CASES)
async def test_protocol_result_carries_no_backend_text(exploding, tool, args, expected):
    result = await mcp_server.mcp.call_tool(tool, args)
    dumped = result.model_dump_json()
    assert "qdrant" not in dumped and "secret" not in dumped
    assert "internal error" in dumped


@pytest.mark.parametrize("uri", ["memory://stats", "memory://all"])
async def test_resources_carry_no_backend_text(exploding, uri):
    contents = list(await mcp_server.mcp.read_resource(uri))
    text = "".join(str(c.content) for c in contents)
    assert "qdrant" not in text and "secret" not in text
    assert "internal error" in text
