"""Per-user access token store for the memory MCP server.

Tokens are random, high-entropy strings — plain SHA-256 (no salt, no slow
KDF) is appropriate here precisely because they are not user-chosen
passwords; a 32-byte random token cannot be brute-forced or dictionary-
attacked the way a password can. Only the hash is ever written to disk.
"""

from __future__ import annotations

import fcntl
import hashlib
import hmac
import json
import os
import re
import secrets
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime

_USER_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")


def validate_user_id(user_id: str) -> str:
    """Return `user_id` if it is safe to use as an id, else raise ValueError.

    fullmatch (not `$`) so a trailing newline is rejected.
    """
    if not isinstance(user_id, str) or not _USER_ID_RE.fullmatch(user_id):
        raise ValueError(
            f"invalid user_id {user_id!r}: must match [A-Za-z0-9][A-Za-z0-9_-]{{0,63}}"
        )
    return user_id


def generate_user_id() -> str:
    return "cust_" + secrets.token_hex(8)


def generate_token(user_id: str) -> str:
    return f"ms_{user_id}_{secrets.token_urlsafe(32)}"


def hash_token(token: str) -> str:
    return "sha256:" + hashlib.sha256(token.encode("utf-8")).hexdigest()


class UserStore:
    def __init__(self, path: str) -> None:
        self._path = path

    def load(self) -> list[dict]:
        if not os.path.exists(self._path):
            return []
        with open(self._path, encoding="utf-8") as f:
            return json.load(f)

    @contextmanager
    def _locked(self) -> Iterator[None]:
        """Exclusive cross-process lock held across a whole read-modify-write."""
        parent = os.path.dirname(self._path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(self._path + ".lock", "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def save(self, users: list[dict]) -> None:
        """Atomically replace the file: temp file in the same dir, then rename."""
        parent = os.path.dirname(self._path) or "."
        os.makedirs(parent, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=parent, prefix=".users-", suffix=".tmp")  # mode 0600
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(users, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self._path)
        except BaseException:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass
            raise

    def resolve(self, token: str) -> str | None:
        token_hash = hash_token(token)
        # Loaded on every request (not cached) so a revoke takes effect at once.
        for u in self.load():
            if hmac.compare_digest(u["token_hash"], token_hash) and not u.get("revoked", False):
                return u["user_id"]
        return None

    def add_user(self, user_id: str, display_name: str) -> str:
        # An empty / whitespace-only user_id would mint a real, working token
        # whose resolve() returns a falsy string — which used to fall through
        # to the configured default user. Reject it at the source.
        if not user_id or not user_id.strip():
            raise ValueError("user_id must be a non-empty, non-whitespace string")
        validate_user_id(user_id)
        with self._locked():
            return self._add_user_locked(user_id, display_name)

    def _add_user_locked(self, user_id: str, display_name: str) -> str:
        users = self.load()
        # user_id is unique across ALL rows, revoked or not. Allowing a second
        # row for a previously-revoked user_id silently broke revoke_user() and
        # rotate_token(), which both stop at the first matching row: revoking
        # again re-revoked the dead row and left the live token working, and
        # rotating resurrected the dead row's token alongside the live one.
        # The way to give a revoked user a working token again is rotate_token().
        for u in users:
            if u["user_id"] == user_id:
                if u.get("revoked", False):
                    raise ValueError(
                        f"user_id {user_id!r} already exists (revoked) — "
                        f"use `rotate {user_id}` to issue a new token"
                    )
                raise ValueError(f"user_id {user_id!r} already exists")
        token = generate_token(user_id)
        users.append(
            {
                "user_id": user_id,
                "display_name": display_name,
                "token_hash": hash_token(token),
                "created_at": datetime.now(UTC).isoformat(),
                "revoked": False,
            }
        )
        self.save(users)
        return token

    def rotate_token(self, user_id: str) -> str:
        """Issue a new token for `user_id`, invalidating the old one. Also
        un-revokes the user — this is the supported way to give a revoked user
        a working token again, since add_user() refuses a duplicate user_id.
        """
        with self._locked():
            return self._rotate_locked(user_id)

    def _rotate_locked(self, user_id: str) -> str:
        users = self.load()
        matches = [u for u in users if u["user_id"] == user_id]
        if not matches:
            raise ValueError(f"user_id {user_id!r} not found")
        token = generate_token(user_id)
        matches[0]["token_hash"] = hash_token(token)
        matches[0]["revoked"] = False
        # add_user() now enforces one row per user_id, so `matches` is normally
        # a single row. Revoking any extras defends a store written by an older
        # version, which could contain duplicate rows — never leave two live
        # tokens for one user_id.
        for u in matches[1:]:
            u["revoked"] = True
        self.save(users)
        return token

    def revoke_user(self, user_id: str) -> None:
        with self._locked():
            self._revoke_locked(user_id)

    def _revoke_locked(self, user_id: str) -> None:
        users = self.load()
        matches = [u for u in users if u["user_id"] == user_id]
        if not matches:
            raise ValueError(f"user_id {user_id!r} not found")
        # Revoke every matching row, not just the first — see rotate_token().
        for u in matches:
            u["revoked"] = True
        self.save(users)

    def list_users(self) -> list[dict]:
        return [{k: v for k, v in u.items() if k != "token_hash"} for u in self.load()]
