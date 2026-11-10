"""Mem0 construction and the startup vector-dimension check.

mem0 is imported lazily inside build_memory(): importing it reads
MEM0_TELEMETRY once, so the server must set that variable first.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from collections.abc import Iterable

from config import Config, ConfigError
from users import validate_user_id

logger = logging.getLogger(__name__)


def telemetry_env(mem0_telemetry: bool) -> dict[str, str]:
    """Environment variables that switch library telemetry off (Mem0's only if
    `mem0_telemetry` is false). Apply them before anything imports mem0.

    - MEM0_TELEMETRY: Mem0 reads it once, when it is first imported.
    - ORT_DISABLE_TELEMETRY: importing mem0 imports fastembed and onnxruntime
      1.29, whose native library starts Microsoft telemetry at import time
      (a device id and an event queue under ~/.cache/Microsoft, uploaded to
      Microsoft). It reads this variable then; onnxruntime's
      disable_telemetry_events(), called after the import, is too late for the
      events already queued. Always off: memsilo has no use for it.
    - HF_HUB_DISABLE_TELEMETRY: huggingface_hub, which fastembed uses to fetch
      models.
    """
    return {
        "MEM0_TELEMETRY": "true" if mem0_telemetry else "false",
        "ORT_DISABLE_TELEMETRY": "1",
        "HF_HUB_DISABLE_TELEMETRY": "1",
    }


def build_memory(cfg: Config):
    """Build the Mem0 `Memory` described by `cfg`."""
    from mem0 import Memory

    qdrant: dict = {
        "collection_name": cfg.collection,
        "url": cfg.qdrant_url,
        "embedding_model_dims": cfg.embedder_dims,
    }
    if cfg.qdrant_api_key:
        qdrant["api_key"] = cfg.qdrant_api_key
    else:
        # mem0's QdrantConfig rejects a bare url unless api_key or path is also
        # present. With url set the client ignores path, so this only satisfies
        # the validator for a key-less Qdrant.
        qdrant["path"] = "/tmp/qdrant"

    parent = os.path.dirname(cfg.history_db)
    if parent:
        os.makedirs(parent, exist_ok=True)

    return Memory.from_config(
        {
            "history_db_path": cfg.history_db,
            "vector_store": {"provider": "qdrant", "config": qdrant},
            "llm": {
                "provider": cfg.llm_provider,
                "config": {"model": cfg.llm_model, "api_key": cfg.llm_api_key},
            },
            "embedder": {
                "provider": cfg.embedder_provider,
                "config": {"model": cfg.embedder_model},
            },
        }
    )


def _vector_size(vectors) -> int:
    """Size of the collection's vectors: a VectorParams, or a dict of named ones."""
    if isinstance(vectors, dict):
        sizes = {getattr(v, "size", None) for v in vectors.values()}
        if len(sizes) == 1 and None not in sizes:
            return sizes.pop()
        raise ConfigError(
            f"Cannot determine the vector size of the Qdrant collection from named vectors {sorted(vectors)}."
        )
    size = getattr(vectors, "size", None)
    if isinstance(size, int):
        return size
    raise ConfigError(
        "Cannot determine the vector size of the Qdrant collection (unrecognised config)."
    )


def verify_collection_dims(client, collection: str, dims: int) -> None:
    """Refuse to start when an existing collection's vector size differs from `dims`."""
    if not client.collection_exists(collection):
        return
    vectors = client.get_collection(collection).config.params.vectors
    actual = _vector_size(vectors)
    if actual != dims:
        raise ConfigError(
            f"Qdrant collection '{collection}' has vectors of size {actual}, but the "
            f"configured embedder produces size {dims}. Use a new `collection` name "
            f"or remove the old collection."
        )


def _results(raw) -> list:
    """Mem0's get_all returns {"results": [...]} (or, in older builds, a list)."""
    if isinstance(raw, dict):
        raw = raw.get("results", [])
    return list(raw or [])


def _check_user_id(user_id: str) -> None:
    """Refuse anything that is not a well-formed memsilo user id, before any
    store is touched.

    Same rule as the user store (users.validate_user_id). Reject, never trim:
    mem0 strips entity ids, so " alice " would reach alice's memories, and
    mem0's Qdrant store treats a user_id of "*" as match-any, so "*" would
    reach EVERY user's memories.
    """
    validate_user_id(user_id)


def _all(mem, user_id: str) -> list:
    """Every memory of `user_id`, including expired ones.

    Mem0's get_all hides memories past their `expiration_date` unless
    show_expired=True, but delete_all removes them all the same, and their
    text is in the history database either way: erase and export must see them.
    """
    return _results(mem.get_all(filters={"user_id": user_id}, top_k=100_000, show_expired=True))


def count_user(mem, user_id: str) -> int:
    """How many memories Mem0 currently holds for `user_id`."""
    _check_user_id(user_id)
    return len(_all(mem, user_id))


_LISTED_IDS = 200  # ids spelled out in the error message; `memory_ids` has them all


class EraseIncomplete(RuntimeError):
    """Text of an erased customer may still be in the history database.

    Raised when the purge failed or the post-purge check found rows left. The
    memories may already be gone from Qdrant, and then a later erase cannot
    find these ids again, so the caller must not be told to simply re-run:
    `memory_ids` (all of them) is what a manual purge needs.
    """

    def __init__(
        self,
        user_id: str,
        memory_ids: list[str],
        db_path: str,
        *,
        history_rows: int | None = None,
        message_rows: int | None = None,
        cause: BaseException | None = None,
        purge_error: BaseException | None = None,
    ) -> None:
        self.user_id = user_id
        self.memory_ids = list(memory_ids)
        self.db_path = db_path
        self.history_rows = history_rows
        self.message_rows = message_rows
        shown = ", ".join(self.memory_ids[:_LISTED_IDS]) or "none"
        more = len(self.memory_ids) - _LISTED_IDS
        if more > 0:
            shown += f" and {more} more"
        parts = []
        if cause is not None:
            parts.append(f"deleting the memories failed ({cause})")
        if purge_error is not None:
            parts.append(
                f"could not purge or verify {db_path} ({type(purge_error).__name__}: {purge_error})"
            )
        else:
            parts.append(
                f"{history_rows} history rows and {message_rows} cached message rows of "
                f"{user_id!r} remain in {db_path} after the purge"
            )
        super().__init__(
            "; ".join(parts) + "; a later erase cannot find these ids once the memories are "
            f"gone from Qdrant ({len(self.memory_ids)} memory ids: {shown})"
        )


def _json_ids(memory_ids: Iterable[str]) -> str:
    """The id list as ONE bound parameter, a JSON array that SQLite's json_each
    unpacks: the SQL text stays constant and the bound-variable limit does not
    apply."""
    return json.dumps([str(i) for i in memory_ids])


def purge_history(db_path: str, memory_ids: Iterable[str]) -> int:
    """Delete every history row (ADD, UPDATE and DELETE) for `memory_ids`.

    Mem0 keeps the full text of each memory in its SQLite history table, and a
    delete only adds one more row. Returns the rows removed; 0 if the file is
    absent. `secure_delete` zeroes the freed bytes instead of leaving them.
    """
    ids = list(memory_ids)
    if not ids or not os.path.exists(db_path):
        return 0
    con = sqlite3.connect(db_path)
    try:
        con.execute("PRAGMA secure_delete=ON")
        with con:
            return con.execute(
                "DELETE FROM history WHERE memory_id IN (SELECT value FROM json_each(?))",
                (_json_ids(ids),),
            ).rowcount
    finally:
        con.close()


def _session_scope(user_id: str) -> str:
    """Mem0's session_scope for a user-only session (mem0.memory.main.
    _build_session_scope). Its escaping of % & = is a no-op for a validated id;
    the tests build the scope with mem0's own function, so a format change in
    mem0 fails them rather than leaving this matching nothing."""
    return f"user_id={user_id}"


def remaining_rows(db_path: str, memory_ids: Iterable[str], user_id: str) -> tuple[int, int]:
    """(history rows for `memory_ids`, cached message rows for `user_id`) still in the db."""
    _check_user_id(user_id)
    if not os.path.exists(db_path):
        return 0, 0
    ids = list(memory_ids)
    con = sqlite3.connect(db_path)
    try:
        messages = con.execute(
            "SELECT count(*) FROM messages WHERE session_scope = ?", (_session_scope(user_id),)
        ).fetchone()[0]
        history = 0
        if ids:
            history = con.execute(
                "SELECT count(*) FROM history WHERE memory_id IN (SELECT value FROM json_each(?))",
                (_json_ids(ids),),
            ).fetchone()[0]
        return history, messages
    finally:
        con.close()


def purge_messages(db_path: str, user_id: str) -> int:
    """Delete the raw conversation messages Mem0 caches for this user's session."""
    _check_user_id(user_id)
    if not os.path.exists(db_path):
        return 0
    con = sqlite3.connect(db_path)
    try:
        con.execute("PRAGMA secure_delete=ON")
        with con:
            return con.execute(
                "DELETE FROM messages WHERE session_scope = ?", (_session_scope(user_id),)
            ).rowcount
    finally:
        con.close()


def erase_user(mem, user_id: str, history_db_path: str | None = None) -> int:
    """Delete every memory belonging to `user_id`; return how many there were.

    With `history_db_path`, also purges that user's rows from Mem0's history
    database (ids are collected before the delete, which writes DELETE rows
    carrying the full text), then re-reads the database. If the purge fails or
    any of those rows remain, raises EraseIncomplete listing the ids: once the
    memories are gone from Qdrant a later erase cannot find them again.

    If delete_all itself fails and the purge of every collected id succeeded
    and verified, the original error is re-raised: the memories it did not
    delete are still listed, so a re-run picks them up.
    """
    _check_user_id(user_id)
    items = _all(mem, user_id)
    ids = [r.get("id") for r in items if isinstance(r, dict) and r.get("id")]
    try:
        mem.delete_all(user_id=user_id)
    except Exception as e:
        # Even when delete_all fails partway, Mem0 has already written DELETE
        # rows (full text) for what it did remove, and a re-run will no longer
        # list those ids - so purge every id collected above now.
        if history_db_path:
            _purge_and_verify(history_db_path, ids, user_id, cause=e)
        raise
    if history_db_path:
        _purge_and_verify(history_db_path, ids, user_id)
    return len(items)


def _purge_and_verify(
    db_path: str, ids: list[str], user_id: str, cause: BaseException | None = None
) -> None:
    try:
        purge_history(db_path, ids)
        purge_messages(db_path, user_id)
        history, messages = remaining_rows(db_path, ids, user_id)
    except Exception as e:
        logger.exception("history purge failed during erase")
        raise EraseIncomplete(user_id, ids, db_path, cause=cause, purge_error=e) from e
    if history or messages:
        raise EraseIncomplete(
            user_id, ids, db_path, history_rows=history, message_rows=messages, cause=cause
        ) from cause


def export_user(mem, user_id: str) -> list[dict]:
    """Return all of `user_id`'s memories as plain dicts."""
    _check_user_id(user_id)
    return [
        {
            "id": r.get("id", ""),
            "memory": r.get("memory", ""),
            "created_at": r.get("created_at", ""),
            "metadata": r.get("metadata") or {},
        }
        for r in _all(mem, user_id)
    ]
