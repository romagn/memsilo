import json

import mcp_server

_ID = "3f2b8c1e-7a4d-4e5f-9b6a-1c2d3e4f5a6b"  # a well-formed Mem0 id


class _FakeMem0WithGet:
    def __init__(self):
        self.deleted = []

    def get(self, memory_id):
        return {"id": memory_id, "user_id": "alice", "memory": "alice's fact"}

    def delete(self, memory_id):
        self.deleted.append(memory_id)


def test_delete_refuses_when_not_owner(monkeypatch):
    fake = _FakeMem0WithGet()
    monkeypatch.setattr(mcp_server, "_mem0", lambda: fake)
    tok = mcp_server.current_user_id.set("bob")
    try:
        result = json.loads(mcp_server.delete_memory(_ID))
    finally:
        mcp_server.current_user_id.reset(tok)
    assert result["deleted"] is False
    assert fake.deleted == []


def test_delete_succeeds_for_owner(monkeypatch):
    fake = _FakeMem0WithGet()
    monkeypatch.setattr(mcp_server, "_mem0", lambda: fake)
    tok = mcp_server.current_user_id.set("alice")
    try:
        result = json.loads(mcp_server.delete_memory(_ID))
    finally:
        mcp_server.current_user_id.reset(tok)
    assert result["deleted"] is True
    assert fake.deleted == [_ID]
