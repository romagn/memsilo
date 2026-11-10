"""Shared test doubles: an in-memory Mem0 stand-in and a Qdrant client stand-in."""

import uuid
from types import SimpleNamespace

_ENTITY_KEYS = ("user_id", "agent_id", "run_id")
_NO_ENTITY = "filters must contain at least one of: user_id, agent_id, run_id"


def _trim(value):
    """Like mem0's _validate_and_trim_entity_id: coerce to str, strip."""
    return None if value is None else str(value).strip()


class FakeMemory:
    """Mem0 2.0.20 stand-in, scoped by user_id.

    Signatures mirror mem0.memory.main.Memory where mcp_server.py and the CLI
    depend on them: ``search(query, *, top_k, filters, **kwargs)`` (unknown
    kwargs such as the old ``limit`` are silently swallowed, exactly as mem0
    does), keyword-only ``add``/``get_all``, entity ids trimmed, and a
    ValueError when no entity filter is given.

    Two Qdrant/mem0 behaviours are reproduced on purpose, so that a regression
    in memsilo's own guards would do real damage here rather than pass quietly:

    - a user_id of ``"*"`` is a wildcard that matches EVERY user (mem0's Qdrant
      store drops a ``"*"`` condition: vector_stores/qdrant.py ``_create_filter``),
      for get_all, search and delete_all alike;
    - an entry with ``"expired": True`` is hidden from get_all/search unless
      ``show_expired=True`` (mem0's ``_payload_is_expired`` filter), yet
      delete_all still removes it (it lists the vector store directly).

    Every call is recorded in ``calls`` as ``(method name, kwargs)``.
    """

    def __init__(self):
        self.store: dict[str, list[dict]] = {}  # user_id -> entries
        self._n = 0
        self.calls: list[tuple[str, dict]] = []

    def add(
        self,
        messages,
        *,
        user_id=None,
        agent_id=None,
        run_id=None,
        metadata=None,
        infer=True,
        **kwargs,
    ):
        self.calls.append(("add", {"user_id": user_id, "metadata": metadata}))
        user_id = _trim(user_id)
        self._n += 1
        # mem0 drops identity keys from caller metadata (_strip_identity_keys).
        meta = {k: v for k, v in (metadata or {}).items() if k not in _ENTITY_KEYS}
        # Mem0 ids are str(uuid.uuid4()); delete_memory refuses anything else.
        entry = {"id": str(uuid.uuid4()), "memory": messages, "user_id": user_id}
        if meta:
            entry["metadata"] = meta
        self.store.setdefault(user_id, []).append(entry)
        return {"results": [dict(entry)]}

    @staticmethod
    def _scope(filters) -> str | None:
        found = {k: _trim(v) for k, v in (filters or {}).items() if k in _ENTITY_KEYS and v}
        if not found:
            raise ValueError(_NO_ENTITY)
        return found.get("user_id")

    def _entries(self, uid, show_expired=False) -> list[dict]:
        if uid == "*":  # Qdrant wildcard: no user_id condition at all
            found = [e for entries in self.store.values() for e in entries]
        else:
            found = list(self.store.get(uid, []))
        return [e for e in found if show_expired or not e.get("expired")]

    def search(self, query, *, top_k=20, filters=None, show_expired=False, **kwargs):
        self.calls.append(("search", {"filters": filters, "top_k": top_k, **kwargs}))
        uid = self._scope(filters)
        return {"results": [{**e, "score": 1.0} for e in self._entries(uid, show_expired)][:top_k]}

    def get_all(self, *, filters=None, top_k=20, show_expired=False, **kwargs):
        self.calls.append(("get_all", {"filters": filters, "show_expired": show_expired}))
        uid = self._scope(filters)
        return {"results": [dict(e) for e in self._entries(uid, show_expired)][:top_k]}

    def get(self, memory_id):
        self.calls.append(("get", {"memory_id": memory_id}))
        for entries in self.store.values():
            for e in entries:
                if e["id"] == memory_id:
                    return dict(e)
        return None

    def delete(self, memory_id):
        self.calls.append(("delete", {"memory_id": memory_id}))
        for entries in self.store.values():
            for e in entries:
                if e["id"] == memory_id:
                    entries.remove(e)
                    return {"message": "Memory deleted successfully!"}
        raise ValueError(f"Memory with id {memory_id} not found")

    def delete_all(self, user_id=None, agent_id=None, run_id=None):
        self.calls.append(("delete_all", {"user_id": user_id}))
        user_id = _trim(user_id)
        if not any([user_id, agent_id, run_id]):
            raise ValueError("At least one filter is required to delete all memories.")
        if user_id == "*":  # Qdrant wildcard: every user's memories go
            self.store.clear()
        else:
            self.store.pop(user_id, None)
        return {"message": "Memories deleted successfully!"}


class FakeQdrant:
    """The slice of QdrantClient that verify_collection_dims uses.

    `vectors` is whatever `config.params.vectors` should hold: a VectorParams-like
    object (anything with `.size`) or a dict of named vectors.
    """

    def __init__(self, collections: dict | None = None):
        self.collections = collections or {}

    @classmethod
    def with_size(cls, name: str, size: int):
        return cls({name: SimpleNamespace(size=size)})

    def collection_exists(self, collection_name):
        return collection_name in self.collections

    def get_collection(self, collection_name):
        vectors = self.collections[collection_name]
        return SimpleNamespace(config=SimpleNamespace(params=SimpleNamespace(vectors=vectors)))
