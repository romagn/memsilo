# tests/test_users.py
import json
import multiprocessing
import os
import stat

import pytest

import users as users_module
from users import UserStore, generate_user_id, hash_token, validate_user_id


@pytest.fixture
def store(tmp_path):
    return UserStore(str(tmp_path / "users.json"))


def test_add_user_then_resolve(store):
    token = store.add_user("alice", "Alice")
    assert token.startswith("ms_alice_")
    assert store.resolve(token) == "alice"


def test_resolve_unknown_token_returns_none(store):
    store.add_user("alice", "Alice")
    assert store.resolve("ms_nope_garbage") is None


def test_resolve_revoked_token_returns_none(store):
    token = store.add_user("alice", "Alice")
    store.revoke_user("alice")
    assert store.resolve(token) is None


def test_only_hash_is_persisted(store):
    token = store.add_user("alice", "Alice")
    users = store.load()
    assert len(users) == 1
    assert users[0]["token_hash"] == hash_token(token)
    assert token not in json.dumps(users)


def test_add_user_duplicate_raises(store):
    store.add_user("alice", "Alice")
    with pytest.raises(ValueError):
        store.add_user("alice", "Alice Again")


def test_add_user_after_revoke_raises(store):
    """user_id is unique across ALL rows, revoked or not — re-adding a revoked
    user_id used to append a second row, which silently broke revoke_user() and
    rotate_token() (both stop at the first matching row)."""
    store.add_user("alice", "Alice")
    store.revoke_user("alice")
    with pytest.raises(ValueError) as exc:
        store.add_user("alice", "Alice Again")
    # The message must point the operator at the supported way back in.
    assert "rotate" in str(exc.value)
    assert len(store.load()) == 1


def test_rotate_after_revoke_gives_one_working_token(store):
    """The supported way to give a revoked user a working token again."""
    old_token = store.add_user("alice", "Alice")
    store.revoke_user("alice")
    assert store.resolve(old_token) is None

    new_token = store.rotate_token("alice")
    assert store.resolve(new_token) == "alice"
    assert store.resolve(old_token) is None
    users = store.load()
    assert len(users) == 1
    assert users[0]["revoked"] is False


def test_revoke_after_readd_attempt_still_kills_the_live_token(store):
    """The original bug's exact sequence: add → revoke → (re-add now refused,
    so) rotate → revoke. The second revoke must actually kill the live token
    rather than re-revoking a stale duplicate row."""
    store.add_user("alice", "Alice")
    store.revoke_user("alice")
    second_token = store.rotate_token("alice")
    assert store.resolve(second_token) == "alice"

    store.revoke_user("alice")
    assert store.resolve(second_token) is None
    assert all(u["revoked"] for u in store.load())


def test_only_one_row_per_user_id_ever(store):
    store.add_user("alice", "Alice")
    for _ in range(3):
        store.revoke_user("alice")
        with pytest.raises(ValueError):
            store.add_user("alice", "Alice")
        store.rotate_token("alice")
    user_ids = [u["user_id"] for u in store.load()]
    assert user_ids == ["alice"]


@pytest.mark.parametrize("bad", ["", "   ", "\t\n"])
def test_add_user_rejects_empty_or_whitespace_user_id(store, bad):
    """An empty user_id used to mint a working token whose resolve() returned
    "" — which resolve_user_id() then treated as absent, silently escalating
    the caller to the configured default user."""
    with pytest.raises(ValueError):
        store.add_user(bad, "Sneaky")
    assert store.load() == []


def test_rotate_token_invalidates_old_token(store):
    old_token = store.add_user("alice", "Alice")
    new_token = store.rotate_token("alice")
    assert new_token != old_token
    assert store.resolve(old_token) is None
    assert store.resolve(new_token) == "alice"


def test_rotate_unknown_user_raises(store):
    with pytest.raises(ValueError):
        store.rotate_token("nobody")


def test_revoke_unknown_user_raises(store):
    with pytest.raises(ValueError):
        store.revoke_user("nobody")


def test_list_users_never_includes_token_hash(store):
    store.add_user("alice", "Alice")
    listing = store.list_users()
    assert listing[0]["user_id"] == "alice"
    assert "token_hash" not in listing[0]


def test_load_missing_file_returns_empty_list(tmp_path):
    store = UserStore(str(tmp_path / "does_not_exist.json"))
    assert store.load() == []


@pytest.mark.parametrize("bad", ["", "  ", "../x", "a b", "a" * 65, "é", "x\n"])
def test_rejects_bad_user_ids(store, bad):
    with pytest.raises(ValueError):
        store.add_user(bad, "Bad")
    assert store.load() == []
    with pytest.raises(ValueError):
        validate_user_id(bad)


@pytest.mark.parametrize("good", ["alice", "a", "A1", "cust_0123abcd", "a-b_c", "a" * 64])
def test_validate_user_id_accepts_good_ids(good):
    assert validate_user_id(good) == good


def test_generate_user_id_is_valid_and_unique():
    ids = [generate_user_id() for _ in range(1000)]
    assert all(validate_user_id(i) == i for i in ids)
    assert all(i.startswith("cust_") for i in ids)
    assert len(set(ids)) == 1000


def test_save_is_atomic(store, tmp_path, monkeypatch):
    store.save([{"user_id": "first"}])

    def boom(obj, fp, *args, **kwargs):
        fp.write("{partial")
        raise OSError("disk full")

    monkeypatch.setattr(users_module.json, "dump", boom)
    with pytest.raises(OSError):
        store.save([{"user_id": "second"}])
    monkeypatch.undo()

    assert store.load() == [{"user_id": "first"}]
    assert sorted(os.listdir(tmp_path)) == ["users.json"]


def test_users_file_mode_is_0600(store, tmp_path):
    store.add_user("alice", "Alice")
    assert stat.S_IMODE(os.stat(tmp_path / "users.json").st_mode) == 0o600
    store.rotate_token("alice")
    assert stat.S_IMODE(os.stat(tmp_path / "users.json").st_mode) == 0o600


# The workers live at module level and use the "fork" context deliberately:
# the child inherits this already-imported module, so it does not depend on
# sys.path / rootdir re-import under "spawn" (the cause of flaky pytest +
# multiprocessing setups). The test process is single-threaded, so fork is safe.
_CTX = multiprocessing.get_context("fork")


def _add_worker(path, user_id):
    UserStore(path).add_user(user_id, user_id)


def _mixed_worker(path, user_id):
    s = UserStore(path)
    s.rotate_token(user_id)
    s.revoke_user(user_id)
    s.add_user(user_id + "_new", "New")


def _run(target, path, ids):
    procs = [_CTX.Process(target=target, args=(path, i)) for i in ids]
    for p in procs:
        p.start()
    for p in procs:
        p.join(60)
    assert [p.exitcode for p in procs] == [0] * len(procs)


def test_concurrent_adds_keep_every_user(tmp_path):
    path = str(tmp_path / "users.json")
    ids = [f"user{i}" for i in range(8)]
    _run(_add_worker, path, ids)
    store = UserStore(path)
    with open(path, encoding="utf-8") as f:
        json.load(f)
    assert sorted(u["user_id"] for u in store.load()) == sorted(ids)


def test_concurrent_mixed_operations_keep_every_user(tmp_path):
    path = str(tmp_path / "users.json")
    store = UserStore(path)
    base = [f"base{i}" for i in range(6)]
    for b in base:
        store.add_user(b, b)
    _run(_mixed_worker, path, base)
    rows = {u["user_id"]: u for u in store.load()}
    assert set(rows) == set(base) | {b + "_new" for b in base}
    assert all(rows[b]["revoked"] for b in base)
    assert not any(rows[b + "_new"]["revoked"] for b in base)


def test_resolve_compares_hashes_in_constant_time(store, monkeypatch):
    """Token hashes are compared with hmac.compare_digest, never `==`."""
    import hmac

    token = store.add_user("alice", "Alice")
    seen = []
    real = hmac.compare_digest

    def spy(a, b):
        seen.append((a, b))
        return real(a, b)

    monkeypatch.setattr(users_module.hmac, "compare_digest", spy)
    assert store.resolve(token) == "alice"
    assert (store.load()[0]["token_hash"], hash_token(token)) in seen or (
        hash_token(token),
        store.load()[0]["token_hash"],
    ) in seen
