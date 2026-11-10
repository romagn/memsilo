# tests/test_manage_users.py
import dataclasses
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from memory import (
    EraseIncomplete,
    count_user,
    erase_user,
    export_user,
    purge_history,
    purge_messages,
)
from scripts import manage_users
from tests.fakes import FakeMemory
from users import UserStore, validate_user_id

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "manage_users.py"


@pytest.fixture
def users_file(tmp_path, monkeypatch):
    path = str(tmp_path / "users.json")
    monkeypatch.setattr(manage_users, "USERS_FILE", path)
    return path


@pytest.fixture(autouse=True)
def history_path(tmp_path, monkeypatch):
    """Never let a CLI test purge the working directory's real data/history.db."""
    path = str(tmp_path / "history.db")
    monkeypatch.setattr(
        manage_users, "config", dataclasses.replace(manage_users.config, history_db=path)
    )
    return path


def test_add_prints_token_once(users_file, capsys):
    rc = manage_users.main(["add", "alice", "--display-name", "Alice"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "ms_alice_" in out
    store = UserStore(users_file)
    assert store.list_users()[0]["user_id"] == "alice"


def test_add_duplicate_fails_with_message(users_file, capsys):
    manage_users.main(["add", "alice", "--display-name", "Alice"])
    rc = manage_users.main(["add", "alice", "--display-name", "Alice"])
    assert rc == 1
    assert "already exists" in capsys.readouterr().out


def test_rotate_prints_new_token(users_file, capsys):
    manage_users.main(["add", "alice", "--display-name", "Alice"])
    capsys.readouterr()
    rc = manage_users.main(["rotate", "alice"])
    assert rc == 0
    assert "ms_alice_" in capsys.readouterr().out


def test_revoke_marks_revoked(users_file, capsys):
    manage_users.main(["add", "alice", "--display-name", "Alice"])
    rc = manage_users.main(["revoke", "alice"])
    assert rc == 0
    store = UserStore(users_file)
    assert store.list_users()[0]["revoked"] is True


def test_list_shows_users_without_tokens(users_file, capsys):
    manage_users.main(["add", "alice", "--display-name", "Alice"])
    capsys.readouterr()
    rc = manage_users.main(["list"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "alice" in out
    assert "ms_" not in out


def test_unknown_user_for_rotate_fails(users_file, capsys):
    rc = manage_users.main(["rotate", "nobody"])
    assert rc == 1
    assert "not found" in capsys.readouterr().out


def test_add_revoked_user_points_at_rotate(users_file, capsys):
    manage_users.main(["add", "alice", "--display-name", "Alice"])
    manage_users.main(["revoke", "alice"])
    capsys.readouterr()
    rc = manage_users.main(["add", "alice", "--display-name", "Alice"])
    out = capsys.readouterr().out
    assert rc == 1
    assert "already exists" in out
    assert "rotate alice" in out


def test_users_file_argument_bypasses_config_entirely(tmp_path, monkeypatch, capsys):
    """`--users-file` is read straight off the command line, never through
    `config.users_file` — so nothing that feeds config (MEMSILO_USERS_FILE, and
    the repo-root `.env`)
    can redirect the write. Pinned by pointing USERS_FILE at a path whose
    parent does not exist: if it were consulted, this would fail."""
    monkeypatch.setattr(manage_users, "USERS_FILE", "/nonexistent-dir/must-not-be-used.json")
    chosen = tmp_path / "chosen.json"
    rc = manage_users.main(["--users-file", str(chosen), "add", "alice", "--display-name", "Alice"])
    assert rc == 0
    assert "ms_alice_" in capsys.readouterr().out
    assert UserStore(str(chosen)).list_users()[0]["user_id"] == "alice"


def test_add_empty_user_id_fails(users_file, capsys):
    rc = manage_users.main(["add", "", "--display-name", "Sneaky"])
    assert rc == 1
    assert "non-empty" in capsys.readouterr().out


def _run_script(tmp_path, *args, cwd=None):
    """Invoke the script the way the justfile/README/Dockerfile do — directly,
    as `python scripts/manage_users.py ...`, not as an importable module. This
    path used to die with `ModuleNotFoundError: No module named 'config'`
    because sys.path[0] is scripts/, not the repo root, and package-mode=false
    means nothing is installed into the venv.

    Isolation is by `--users-file`, NOT by the MEMSILO_USERS_FILE environment
    variable: `config.py` also reads a `.env` from the current directory (or
    the file named by MEMSILO_DOTENV; empty disables) and a ./config.yaml,
    which is why the env below sets MEMSILO_DOTENV="". Left as an env var, these tests would
    write into a developer's real (possibly production) data/users.json.
    """
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(tmp_path),
        "MEMSILO_QDRANT_URL": "http://127.0.0.1:1",
        "MEMSILO_DOTENV": "",
        # Deliberately pointed somewhere ELSE, so that a regression which
        # stopped honouring --users-file would be caught by a test asserting
        # on the --users-file store rather than silently passing.
        "MEMSILO_USERS_FILE": str(tmp_path / "env-store-must-not-be-used.json"),
    }
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--users-file", str(tmp_path / "users.json"), *args],
        cwd=str(cwd or REPO_ROOT),
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


def test_script_runs_as_a_direct_subprocess(tmp_path):
    result = _run_script(tmp_path, "list")
    assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"
    assert "ModuleNotFoundError" not in result.stderr


def test_script_add_then_list_as_subprocess(tmp_path):
    added = _run_script(tmp_path, "add", "subproc", "--display-name", "Sub Proc")
    assert added.returncode == 0, f"stdout={added.stdout!r} stderr={added.stderr!r}"
    assert "ms_subproc_" in added.stdout

    listed = _run_script(tmp_path, "list")
    assert listed.returncode == 0, listed.stderr
    assert "subproc" in listed.stdout
    assert "ms_" not in listed.stdout

    # The write went where --users-file said, not where MEMSILO_USERS_FILE (or a
    # developer's real .env) pointed.
    assert (tmp_path / "users.json").exists()
    assert not (tmp_path / "env-store-must-not-be-used.json").exists()


def test_users_file_argument_beats_the_environment(tmp_path):
    """`--users-file` must out-rank MEMSILO_USERS_FILE — and therefore also a
    repo-root `.env` or config.yaml setting it. This is what keeps the suite safe to run on a machine
    with a real, populated data/users.json configured."""
    chosen = tmp_path / "chosen.json"
    ignored = tmp_path / "ignored.json"
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--users-file",
            str(chosen),
            "add",
            "precedence",
            "--display-name",
            "Precedence",
        ],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        env={
            "PATH": "/usr/bin:/bin",
            "HOME": str(tmp_path),
            "MEMSILO_USERS_FILE": str(ignored),
            "MEMSILO_QDRANT_URL": "http://127.0.0.1:1",
            "MEMSILO_DOTENV": "",
        },
        check=False,
    )
    assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"
    assert chosen.exists()
    assert not ignored.exists()
    assert UserStore(str(chosen)).list_users()[0]["user_id"] == "precedence"


def test_script_runs_from_another_cwd(tmp_path):
    """`just users ...` runs from the repo root, but the Docker image invokes
    the script by absolute path — the bootstrap must not depend on cwd."""
    result = _run_script(tmp_path, "list", cwd=tmp_path)
    assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"


# ---------------------------------------------------------------------------
# Task 6: --json, bulk-add, erase, export
# ---------------------------------------------------------------------------


def _seed():
    mem = FakeMemory()
    for i in range(3):
        mem.add(f"alice fact {i}", user_id="alice", metadata={"n": i})
    for i in range(2):
        mem.add(f"bob fact {i}", user_id="bob")
    return mem


def _never():
    raise AssertionError("memory factory must not be called")


def test_add_json_has_token_and_valid_id(users_file, capsys):
    rc = manage_users.main(["--json", "add", "alice", "--display-name", "Alice"])
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert set(data) == {"user_id", "display_name", "token"}
    assert validate_user_id(data["user_id"]) == "alice"
    assert data["token"].startswith("ms_alice_")


def test_add_without_id_generates_one(users_file, capsys):
    rc = manage_users.main(["--json", "add", "--display-name", "Anon"])
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert data["user_id"].startswith("cust_")
    validate_user_id(data["user_id"])
    assert UserStore(users_file).list_users()[0]["user_id"] == data["user_id"]


def _csv(tmp_path, text):
    p = tmp_path / "in.csv"
    p.write_text(text, encoding="utf-8")
    return str(p)


def test_bulk_add_creates_all_and_reports_each_token(users_file, tmp_path, capsys):
    f = _csv(tmp_path, "user_id,display_name\nalice,Alice\n,Generated One\nbob,Bob\n")
    rc = manage_users.main(["--json", "bulk-add", f])
    assert rc == 0
    rows = json.loads(capsys.readouterr().out)
    assert len(rows) == 3
    assert rows[0]["user_id"] == "alice" and rows[2]["user_id"] == "bob"
    assert rows[1]["user_id"].startswith("cust_")
    store = UserStore(users_file)
    assert len(store.list_users()) == 3
    for r in rows:
        assert store.resolve(r["token"]) == r["user_id"]


def test_bulk_add_human_output_shows_tokens_once(users_file, tmp_path, capsys):
    f = _csv(tmp_path, "user_id,display_name\nalice,Alice\n")
    assert manage_users.main(["bulk-add", f]) == 0
    out = capsys.readouterr().out
    assert "ms_alice_" in out and "shown once" in out


def test_bulk_add_stops_before_writing_on_a_bad_row(users_file, tmp_path, capsys):
    f = _csv(tmp_path, "user_id,display_name\nalice,Alice\nbad id!,Bad\ncarol,Carol\n")
    rc = manage_users.main(["bulk-add", f])
    assert rc == 1
    assert "row 3" in capsys.readouterr().out
    assert UserStore(users_file).list_users() == []


def test_bulk_add_rejects_duplicate_id_in_file(users_file, tmp_path, capsys):
    f = _csv(tmp_path, "user_id,display_name\nalice,A\nalice,B\n")
    assert manage_users.main(["bulk-add", f]) == 1
    assert "row 3" in capsys.readouterr().out
    assert UserStore(users_file).list_users() == []


def test_bulk_add_rejects_id_already_in_store(users_file, tmp_path, capsys):
    manage_users.main(["add", "alice", "--display-name", "Alice"])
    capsys.readouterr()
    f = _csv(tmp_path, "user_id,display_name\nbob,Bob\nalice,Again\n")
    assert manage_users.main(["bulk-add", f]) == 1
    assert "row 3" in capsys.readouterr().out
    assert [u["user_id"] for u in UserStore(users_file).list_users()] == ["alice"]


def test_bulk_add_rejects_missing_header_and_empty_name(users_file, tmp_path, capsys):
    f = _csv(tmp_path, "alice,Alice\nbob,Bob\n")
    assert manage_users.main(["bulk-add", f]) == 1
    assert "header" in capsys.readouterr().out
    f = _csv(tmp_path, "user_id,display_name\nalice,\n")
    assert manage_users.main(["bulk-add", f]) == 1
    assert "row 2" in capsys.readouterr().out
    assert UserStore(users_file).list_users() == []


def test_erase_requires_yes(users_file):
    mem = _seed()
    rc = manage_users.main(["erase", "alice"], memory_factory=_never)
    assert rc != 0
    assert len(mem.store["alice"]) == 3


def test_erase_removes_only_that_user(users_file, capsys):
    mem = _seed()
    rc = manage_users.main(["--json", "erase", "alice", "--yes"], memory_factory=lambda: mem)
    assert rc == 0
    assert json.loads(capsys.readouterr().out) == {
        "user_id": "alice",
        "erased": 3,
        "revoked": False,
        "remaining": 0,
    }
    assert "alice" not in mem.store
    assert len(mem.store["bob"]) == 2


def test_erase_user_returns_count_and_only_that_user():
    mem = _seed()
    assert erase_user(mem, "alice") == 3
    assert len(mem.store["bob"]) == 2


def test_erase_empty_user_id_does_not_wipe_everyone():
    mem = _seed()
    for bad in ("", "   ", " alice "):
        with pytest.raises(ValueError):
            erase_user(mem, bad)
    assert len(mem.store["alice"]) == 3
    assert len(mem.store["bob"]) == 2


def test_erase_with_revoke_revokes_token(users_file, capsys):
    mem = _seed()
    token = UserStore(users_file).add_user("alice", "Alice")
    rc = manage_users.main(
        ["--json", "erase", "alice", "--yes", "--revoke"], memory_factory=lambda: mem
    )
    assert rc == 0
    assert json.loads(capsys.readouterr().out)["revoked"] is True
    assert UserStore(users_file).resolve(token) is None


def test_erase_unknown_user_returns_zero():
    assert erase_user(_seed(), "nobody") == 0


def test_export_returns_only_that_users_memories(users_file, capsys):
    mem = _seed()
    rc = manage_users.main(["export", "alice"], memory_factory=lambda: mem)
    assert rc == 0
    items = json.loads(capsys.readouterr().out)
    assert len(items) == 3
    assert {i["memory"] for i in items} == {f"alice fact {n}" for n in range(3)}
    assert set(items[0]) == {"id", "memory", "created_at", "metadata"}
    assert [i["memory"] for i in export_user(mem, "bob")] == ["bob fact 0", "bob fact 1"]


def _run_cli(tmp_path, *args):
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(tmp_path),
        "MEMSILO_USERS_FILE": str(tmp_path / "users.json"),
        "MEMSILO_QDRANT_URL": "http://127.0.0.1:1",
        "MEMSILO_DOTENV": "",
    }
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.parametrize("cmd", [["erase", "alice", "--yes"], ["export", "alice"]])
def test_erase_export_without_key_is_clean_error(tmp_path, cmd):
    r = _run_cli(tmp_path, *cmd)
    assert r.returncode == 1
    assert "ANTHROPIC_API_KEY" in r.stdout + r.stderr
    assert "Traceback" not in r.stderr
    assert len((r.stdout + r.stderr).strip().splitlines()) == 1


def test_add_and_list_work_without_key_or_mem0(tmp_path):
    r = _run_cli(tmp_path, "--json", "add", "alice", "--display-name", "Alice")
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout)["token"].startswith("ms_alice_")
    r = _run_cli(tmp_path, "list")
    assert r.returncode == 0 and "alice" in r.stdout
    code = (
        "import sys, runpy; sys.argv=['m','list']\n"
        f"try: runpy.run_path({str(SCRIPT)!r}, run_name='__main__')\n"
        "except SystemExit: pass\n"
        "assert 'mem0' not in sys.modules"
    )
    r = subprocess.run(
        [sys.executable, "-c", code],
        cwd=tmp_path,
        env={
            "PATH": os.environ["PATH"],
            "MEMSILO_USERS_FILE": str(tmp_path / "users.json"),
            "MEMSILO_QDRANT_URL": "http://127.0.0.1:1",
            "MEMSILO_DOTENV": "",
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert r.returncode == 0, r.stderr


# ---------------------------------------------------------------------------
# Fix round 1
# ---------------------------------------------------------------------------


def _telemetry_child(tmp_path, extra_env):
    """Run the CLI's default memory factory with build_memory stubbed (no Qdrant);
    the stub imports mem0's telemetry module, which reads MEM0_TELEMETRY at import."""
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(tmp_path),
        "ANTHROPIC_API_KEY": "dummy",
        "PYTHONPATH": str(REPO_ROOT),
        "MEMSILO_QDRANT_URL": "http://127.0.0.1:1",
        "MEMSILO_DOTENV": "",
    }
    env.update(extra_env)
    code = (
        "import sys\n"
        "sys.path.insert(0, 'scripts')\n"
        "import manage_users, memory\n"
        "def stub(cfg):\n"
        "    import mem0.memory.telemetry as t\n"
        "    return t.MEM0_TELEMETRY\n"
        "memory.build_memory = stub\n"
        "print(manage_users._default_memory_factory())\n"
    )
    r = subprocess.run(
        [sys.executable, "-c", code],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


def test_cli_memory_factory_telemetry_off_by_default(tmp_path):
    assert _telemetry_child(tmp_path, {}) == "False"


def test_cli_memory_factory_telemetry_can_be_enabled(tmp_path):
    assert _telemetry_child(tmp_path, {"MEMSILO_TELEMETRY": "true"}) == "True"


class _Order(FakeMemory):
    def __init__(self, store_path, token):
        super().__init__()
        self.store_path, self.token = store_path, token
        self.revoked_at_delete = None

    def delete_all(self, user_id=None, **kw):
        self.revoked_at_delete = UserStore(self.store_path).resolve(self.token) is None
        return super().delete_all(user_id=user_id, **kw)


def test_erase_revokes_before_erasing_and_reports_remaining(users_file, capsys):
    token = UserStore(users_file).add_user("alice", "Alice")
    mem = _Order(users_file, token)
    for i in range(2):
        mem.add(f"x{i}", user_id="alice")
    rc = manage_users.main(
        ["--json", "erase", "alice", "--yes", "--revoke"], memory_factory=lambda: mem
    )
    assert rc == 0
    assert mem.revoked_at_delete is True
    assert json.loads(capsys.readouterr().out) == {
        "user_id": "alice",
        "erased": 2,
        "revoked": True,
        "remaining": 0,
    }


class _Leaky(FakeMemory):
    def delete_all(self, user_id=None, **kw):
        return {"message": "ok"}  # deletes nothing


def test_erase_fails_when_memories_remain(users_file, capsys):
    mem = _Leaky()
    mem.add("x", user_id="alice")
    rc = manage_users.main(["--json", "erase", "alice", "--yes"], memory_factory=lambda: mem)
    cap = capsys.readouterr()
    assert rc != 0
    assert "remain" in cap.err
    assert json.loads(cap.out)["remaining"] == 1


class _Boom(FakeMemory):
    def delete_all(self, user_id=None, **kw):
        raise RuntimeError("qdrant down")


def test_erase_failure_is_one_line_and_rerun_is_safe(users_file, capsys):
    token = UserStore(users_file).add_user("alice", "Alice")
    mem = _Boom()
    mem.add("x", user_id="alice")
    rc = manage_users.main(["erase", "alice", "--yes", "--revoke"], memory_factory=lambda: mem)
    err = capsys.readouterr().err
    assert rc == 1
    assert len(err.strip().splitlines()) == 1
    assert "already revoked" in err
    assert "`erase alice --yes --revoke`" in err
    assert UserStore(users_file).resolve(token) is None
    good = FakeMemory()
    good.add("x", user_id="alice")
    rc = manage_users.main(["erase", "alice", "--yes", "--revoke"], memory_factory=lambda: good)
    assert rc == 0 and "alice" not in good.store


def test_erase_failure_without_revoke_does_not_advise_revoke(users_file, capsys):
    UserStore(users_file).add_user("alice", "Alice")
    mem = _Boom()
    mem.add("x", user_id="alice")
    rc = manage_users.main(["erase", "alice", "--yes"], memory_factory=lambda: mem)
    err = capsys.readouterr().err
    assert rc == 1
    assert "`erase alice --yes`" in err
    assert "--revoke" not in err and "revoked" not in err


class _Partial(FakeMemory):
    """Deletes one of alice's memories (as mem0 would, writing history), then fails."""

    def delete_all(self, user_id=None, **kw):
        self.store[user_id].pop(0)
        raise RuntimeError("qdrant down")


def _alice_bob_fake(cls=FakeMemory):
    mem = cls()
    mem.store["alice"] = [
        {"id": "a1", "memory": "alice secret one"},
        {"id": "a2", "memory": "alice secret two"},
    ]
    mem.store["bob"] = [{"id": "b1", "memory": "bob note"}]
    return mem


def test_partial_delete_failure_still_purges_all_collected_ids(history_path, capsys):
    _history_db(history_path)
    mem = _alice_bob_fake(_Partial)
    rc = manage_users.main(["erase", "alice", "--yes"], memory_factory=lambda: mem)
    assert rc == 1
    capsys.readouterr()
    # a1 was already deleted by mem0 before the failure; its history must go too.
    assert {r[0] for r in _rows(history_path, "SELECT memory_id FROM history")} == {"b1"}
    assert [r[0] for r in _rows(history_path, "SELECT session_scope FROM messages")] == [
        "user_id=bob"
    ]
    # Re-run (a2 is all that is left) succeeds; bob untouched.
    rerun = FakeMemory()
    rerun.store["alice"] = [{"id": "a2", "memory": "alice secret two"}]
    assert manage_users.main(["erase", "alice", "--yes"], memory_factory=lambda: rerun) == 0
    assert {r[0] for r in _rows(history_path, "SELECT memory_id FROM history")} == {"b1"}


def test_purge_error_does_not_mask_delete_all_error(tmp_path):
    bad = tmp_path / "history.db"
    bad.write_text("not a sqlite database")
    mem = _alice_bob_fake(_Boom)
    with pytest.raises(EraseIncomplete, match="qdrant down") as exc:
        erase_user(mem, "alice", history_db_path=str(bad))
    assert exc.value.memory_ids == ["a1", "a2"]


def test_purge_error_after_successful_delete_is_raised(tmp_path):
    bad = tmp_path / "history.db"
    bad.write_text("not a sqlite database")
    with pytest.raises(EraseIncomplete) as exc:
        erase_user(_alice_bob_fake(), "alice", history_db_path=str(bad))
    assert isinstance(exc.value.__cause__, sqlite3.DatabaseError)
    assert "a1" in str(exc.value) and "could not" in str(exc.value)


def test_cli_passes_config_history_db_to_erase(history_path, capsys):
    """End to end: the CLI purges the file named by config.history_db, nothing else."""
    _history_db(history_path)
    mem = _alice_bob_fake()
    rc = manage_users.main(["--json", "erase", "alice", "--yes"], memory_factory=lambda: mem)
    assert rc == 0
    assert {r[0] for r in _rows(history_path, "SELECT memory_id FROM history")} == {"b1"}


def test_rotate_json(users_file, capsys):
    manage_users.main(["add", "alice", "--display-name", "Alice"])
    capsys.readouterr()
    assert manage_users.main(["--json", "rotate", "alice"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert set(data) == {"user_id", "token"}
    assert UserStore(users_file).resolve(data["token"]) == "alice"


# --- history database purge -------------------------------------------------


def _history_db(path):
    from mem0.memory.storage import SQLiteManager

    db = SQLiteManager(str(path))
    for mid, text, uid in (
        ("a1", "alice secret one", "alice"),
        ("a2", "alice secret two", "alice"),
        ("b1", "bob note", "bob"),
    ):
        db.add_history(mid, None, text, "ADD", actor_id=uid)
        db.add_history(mid, text, None, "DELETE", is_deleted=1, actor_id=uid)
    # The scope string comes from mem0 itself, so a change in mem0's format
    # breaks this test instead of leaving purge_messages silently matching nothing.
    from mem0.memory.main import _build_session_scope

    for uid, text in (("alice", "alice said secret"), ("bob", "bob said hi")):
        db.save_messages(
            [{"role": "user", "content": text}], _build_session_scope({"user_id": uid})
        )
    db.connection.close()


def _rows(path, sql):
    con = sqlite3.connect(str(path))
    try:
        return con.execute(sql).fetchall()
    finally:
        con.close()


def test_erase_user_purges_history_rows(tmp_path):
    db = tmp_path / "history.db"
    _history_db(db)
    mem = FakeMemory()
    mem.store["alice"] = [
        {"id": "a1", "memory": "alice secret one"},
        {"id": "a2", "memory": "alice secret two"},
    ]
    mem.store["bob"] = [{"id": "b1", "memory": "bob note"}]
    assert erase_user(mem, "alice", history_db_path=str(db)) == 2
    assert {r[0] for r in _rows(db, "SELECT memory_id FROM history")} == {"b1"}
    assert (
        _rows(
            db, "SELECT * FROM history WHERE old_memory LIKE '%alice%' OR new_memory LIKE '%alice%'"
        )
        == []
    )
    assert [r[0] for r in _rows(db, "SELECT session_scope FROM messages")] == ["user_id=bob"]
    assert b"alice" not in b"".join(
        str(r).encode()
        for r in _rows(
            db,
            "SELECT * FROM history UNION ALL SELECT id,session_scope,role,content,name,created_at,NULL,NULL,NULL,NULL FROM messages",
        )
    )


def test_purge_history_missing_file_is_noop(tmp_path):
    p = tmp_path / "nope.db"
    assert purge_history(str(p), ["a1"]) == 0
    assert purge_messages(str(p), "alice") == 0
    assert not p.exists()


def test_purge_history_beyond_sqlite_variable_limit(tmp_path):
    from mem0.memory.storage import SQLiteManager

    db = SQLiteManager(str(tmp_path / "h.db"))
    ids = [f"m{i}" for i in range(2500)]
    for i in ids:
        db.add_history(i, None, "t", "ADD")
    db.connection.close()
    assert purge_history(str(tmp_path / "h.db"), ids) == 2500
    assert _rows(tmp_path / "h.db", "SELECT count(*) FROM history") == [(0,)]


def test_build_memory_passes_history_db_path_and_creates_parent(monkeypatch, tmp_path):
    from types import SimpleNamespace

    from config import Config
    from memory import build_memory

    seen = {}
    monkeypatch.setitem(
        sys.modules,
        "mem0",
        SimpleNamespace(Memory=SimpleNamespace(from_config=lambda c: seen.update(c) or "mem")),
    )
    target = tmp_path / "sub" / "dir" / "h.db"
    assert build_memory(Config(llm_api_key="k", history_db=str(target))) == "mem"
    assert seen["history_db_path"] == str(target)
    assert target.parent.is_dir()


def test_count_user():
    assert count_user(_seed(), "alice") == 3


# ---------------------------------------------------------------------------
# Final fix wave: wildcard ids (A2), verified purge (A3), expired memories (A4),
# export of an unknown id (A10)
# ---------------------------------------------------------------------------

BAD_IDS = ["*", "a*", "%", "a b", "../x", "", "   ", " alice ", "ünï", "alice\n"]


def test_fake_memory_treats_star_as_match_any_like_qdrant():
    """The guard tests below are only meaningful if '*' really is a wildcard here."""
    mem = _seed()
    assert len(mem.get_all(filters={"user_id": "*"}, top_k=100)["results"]) == 5
    mem.delete_all(user_id="*")
    assert mem.store == {}


@pytest.mark.parametrize("bad", BAD_IDS)
def test_erase_user_rejects_bad_ids_before_touching_the_store(bad):
    mem = _seed()
    mem.calls.clear()
    with pytest.raises(ValueError):
        erase_user(mem, bad)
    assert mem.calls == []
    assert len(mem.store["alice"]) == 3 and len(mem.store["bob"]) == 2


@pytest.mark.parametrize("fn", [export_user, count_user])
@pytest.mark.parametrize("bad", BAD_IDS)
def test_export_and_count_reject_bad_ids_before_touching_the_store(fn, bad):
    mem = _seed()
    mem.calls.clear()
    with pytest.raises(ValueError):
        fn(mem, bad)
    assert mem.calls == []


def test_purge_messages_rejects_wildcard(tmp_path):
    db = tmp_path / "history.db"
    _history_db(db)
    with pytest.raises(ValueError):
        purge_messages(str(db), "*")
    assert len(_rows(db, "SELECT * FROM messages")) == 2


@pytest.mark.parametrize(
    "argv",
    [
        ["erase", "*", "--yes"],
        ["erase", "*", "--yes", "--revoke"],
        ["--json", "erase", "*", "--yes"],
        ["export", "*"],
        ["erase", "a*", "--yes"],
        ["export", "../x"],
    ],
)
def test_cli_refuses_wildcard_and_bad_ids(users_file, capsys, argv):
    UserStore(users_file).add_user("alice", "Alice")
    before = Path(users_file).read_text()
    rc = manage_users.main(argv, memory_factory=_never)
    cap = capsys.readouterr()
    assert rc == 1
    assert "invalid user_id" in cap.out + cap.err
    assert len((cap.out + cap.err).strip().splitlines()) == 1
    assert Path(users_file).read_text() == before  # nothing revoked


_KEEP_ROWS = {
    "history": "CREATE TRIGGER keep_history BEFORE DELETE ON history BEGIN SELECT RAISE(IGNORE); END",
    "messages": (
        "CREATE TRIGGER keep_messages BEFORE DELETE ON messages BEGIN SELECT RAISE(IGNORE); END"
    ),
}


class _Undeletable:
    """SQLite triggers that silently skip DELETEs, as a stand-in for any purge
    that reports success without removing the rows."""

    @staticmethod
    def install(db, table):
        con = sqlite3.connect(str(db))
        with con:
            con.execute(_KEEP_ROWS[table])
        con.close()


def test_erase_user_names_ids_and_counts_when_history_rows_remain(tmp_path):
    db = tmp_path / "history.db"
    _history_db(db)
    _Undeletable.install(db, "history")
    with pytest.raises(EraseIncomplete) as exc:
        erase_user(_alice_bob_fake(), "alice", history_db_path=str(db))
    msg = str(exc.value)
    assert "\n" not in msg
    assert "4 history rows" in msg and "0 cached message rows" in msg
    assert "a1" in msg and "a2" in msg and "b1" not in msg
    assert "cannot find" in msg  # a re-run cannot re-discover these ids
    assert exc.value.memory_ids == ["a1", "a2"]


def test_erase_user_reports_remaining_messages(tmp_path):
    db = tmp_path / "history.db"
    _history_db(db)
    _Undeletable.install(db, "messages")
    with pytest.raises(EraseIncomplete) as exc:
        erase_user(_alice_bob_fake(), "alice", history_db_path=str(db))
    assert "0 history rows" in str(exc.value) and "1 cached message rows" in str(exc.value)


def test_erase_incomplete_caps_the_printed_id_list(tmp_path):
    from mem0.memory.storage import SQLiteManager

    db = tmp_path / "history.db"
    sm = SQLiteManager(str(db))
    mem = FakeMemory()
    mem.store["alice"] = [{"id": f"m{i:03d}", "memory": "t"} for i in range(250)]
    for e in mem.store["alice"]:
        sm.add_history(e["id"], None, "t", "ADD")
    sm.connection.close()
    _Undeletable.install(db, "history")
    with pytest.raises(EraseIncomplete) as exc:
        erase_user(mem, "alice", history_db_path=str(db))
    msg = str(exc.value)
    assert "m199" in msg and "m200" not in msg and "and 50 more" in msg
    assert "250 history rows" in msg and len(exc.value.memory_ids) == 250


def test_erase_user_fails_when_purge_is_a_no_op(tmp_path, monkeypatch):
    import memory

    db = tmp_path / "history.db"
    _history_db(db)
    monkeypatch.setattr(memory, "purge_history", lambda *a, **k: 0)
    monkeypatch.setattr(memory, "purge_messages", lambda *a, **k: 0)
    with pytest.raises(RuntimeError, match="remain"):
        erase_user(_alice_bob_fake(), "alice", history_db_path=str(db))


def test_cli_erase_exits_1_when_history_rows_remain(history_path, users_file, capsys):
    _history_db(history_path)
    _Undeletable.install(history_path, "history")
    UserStore(users_file).add_user("alice", "cust 001")
    rc = manage_users.main(["erase", "alice", "--yes", "--revoke"], memory_factory=_alice_bob_fake)
    err = capsys.readouterr().err
    assert rc == 1
    assert len(err.strip().splitlines()) == 1
    assert "a1" in err and "a2" in err and "4 history rows" in err
    assert "safe to repeat" not in err and "re-run `erase" not in err
    assert "already revoked" in err
    assert "README" in err and "incomplete erase" in err


def test_cli_erase_json_lists_every_remaining_id(history_path, capsys):
    _history_db(history_path)
    _Undeletable.install(history_path, "history")
    rc = manage_users.main(["--json", "erase", "alice", "--yes"], memory_factory=_alice_bob_fake)
    cap = capsys.readouterr()
    assert rc == 1
    out = json.loads(cap.out)
    assert out["user_id"] == "alice" and out["memory_ids"] == ["a1", "a2"]
    assert out["history_rows"] == 4 and out["message_rows"] == 0


def test_erase_rerun_cannot_rediscover_ids_known_limitation(history_path, capsys):
    """Documented limitation: once the memories are gone from Qdrant, a second
    erase has no ids to look for, so it succeeds although the first run's
    leftover history rows are still there. That is why the first run's error
    lists the ids and the README gives a manual recovery instead of "re-run"."""
    _history_db(history_path)
    _Undeletable.install(history_path, "history")
    assert manage_users.main(["erase", "alice", "--yes"], memory_factory=_alice_bob_fake) == 1
    capsys.readouterr()
    empty = FakeMemory()
    empty.store["bob"] = [{"id": "b1", "memory": "bob note"}]
    assert manage_users.main(["erase", "alice", "--yes"], memory_factory=lambda: empty) == 0
    left = {r[0] for r in _rows(history_path, "SELECT memory_id FROM history")}
    assert {"a1", "a2"} <= left


def test_recovery_snippet_in_readme_purges_listed_ids(history_path):
    """The README's manual recovery, run as written (minus docker) on a real file."""
    import re

    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    block = re.search(r"<<'EOF'\n(.*?)\nEOF\n", readme, re.DOTALL)
    assert block, "recovery snippet not found in README"
    _history_db(history_path)
    r = subprocess.run(
        [sys.executable, "-", "alice", "a1", "a2"],
        input=block.group(1).replace("data/history.db", history_path),
        capture_output=True,
        text=True,
        check=False,
    )
    assert r.returncode == 0, r.stderr
    assert "deleted 4 history rows and 1 cached message rows" in r.stdout
    assert "0 rows remain" in r.stdout
    assert {x[0] for x in _rows(history_path, "SELECT memory_id FROM history")} == {"b1"}


def test_delete_failure_with_unpurgeable_history_is_not_a_plain_rerun(history_path, capsys):
    """delete_all fails after removing a1; its history cannot be purged. A re-run
    would not find a1 again, so the CLI must not promise a safe re-run."""
    _history_db(history_path)
    _Undeletable.install(history_path, "history")
    rc = manage_users.main(
        ["erase", "alice", "--yes"], memory_factory=lambda: _alice_bob_fake(_Partial)
    )
    err = capsys.readouterr().err
    assert rc == 1
    assert "qdrant down" in err and "a1" in err
    assert "safe to repeat" not in err


def _with_expired():
    mem = _alice_bob_fake()
    mem.store["alice"].append({"id": "a3", "memory": "alice expired", "expired": True})
    return mem


def test_count_and_export_include_expired_memories():
    mem = _with_expired()
    assert count_user(mem, "alice") == 3
    assert {i["id"] for i in export_user(mem, "alice")} == {"a1", "a2", "a3"}
    assert all(kw["show_expired"] is True for name, kw in mem.calls if name == "get_all")


def test_erase_purges_history_of_expired_memories(tmp_path):
    from mem0.memory.storage import SQLiteManager

    db = tmp_path / "history.db"
    _history_db(db)
    extra = SQLiteManager(str(db))
    extra.add_history("a3", None, "alice expired", "ADD", actor_id="alice")
    extra.connection.close()
    assert erase_user(_with_expired(), "alice", history_db_path=str(db)) == 3
    assert {r[0] for r in _rows(db, "SELECT memory_id FROM history")} == {"b1"}


def test_export_unknown_id_warns_on_stderr(users_file, capsys):
    rc = manage_users.main(["export", "nobody"], memory_factory=_seed)
    cap = capsys.readouterr()
    assert rc == 0
    assert json.loads(cap.out) == []
    assert "'nobody' is not in the user store" in cap.err
    assert len(cap.err.strip().splitlines()) == 1


def test_export_known_user_with_no_memories_does_not_warn(users_file, capsys):
    UserStore(users_file).add_user("carol", "cust 001")
    rc = manage_users.main(["export", "carol"], memory_factory=_seed)
    cap = capsys.readouterr()
    assert rc == 0 and json.loads(cap.out) == [] and cap.err == ""


def test_export_orphan_id_with_memories_does_not_warn(users_file, capsys):
    rc = manage_users.main(["export", "alice"], memory_factory=_seed)
    cap = capsys.readouterr()
    assert rc == 0 and len(json.loads(cap.out)) == 3 and cap.err == ""


def test_purge_with_no_ids_does_not_open_the_db(tmp_path):
    bad = tmp_path / "history.db"
    bad.write_text("not a sqlite database")  # opening it for real would raise
    assert purge_history(str(bad), []) == 0


def test_purge_and_count_beyond_any_bound_variable_limit(tmp_path):
    """One JSON parameter carries the ids: 40,000 is over SQLite's 32766 cap."""
    from memory import remaining_rows

    db = tmp_path / "history.db"
    _history_db(db)
    ids = ["a1", *(f"x{i}" for i in range(40_000)), "a2"]
    assert remaining_rows(str(db), ids, "alice") == (4, 1)
    assert purge_history(str(db), ids) == 4
    assert remaining_rows(str(db), ids, "alice") == (0, 1)
    assert {r[0] for r in _rows(db, "SELECT memory_id FROM history")} == {"b1"}


def test_ids_with_sql_and_json_metacharacters_are_data(tmp_path):
    from mem0.memory.storage import SQLiteManager

    db = tmp_path / "history.db"
    sm = SQLiteManager(str(db))
    odd = ['a"1', "b') OR 1=1 --", "c\\d", "é"]
    for i in [*odd, "keep"]:
        sm.add_history(i, None, "t", "ADD")
    sm.connection.close()
    assert purge_history(str(db), odd) == 4
    assert _rows(db, "SELECT memory_id FROM history") == [("keep",)]


def test_cli_memory_factory_disables_onnxruntime_telemetry(tmp_path):
    """erase/export build Mem0, whose import loads onnxruntime: its telemetry
    switch must be set first (see tests/test_memory.py for why)."""
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(tmp_path),
        "ANTHROPIC_API_KEY": "dummy",
        "PYTHONPATH": str(REPO_ROOT),
        "MEMSILO_QDRANT_URL": "http://127.0.0.1:1",
        "MEMSILO_DOTENV": "",
        "ORT_DISABLE_TELEMETRY": "0",
    }
    code = (
        "import os, sys\n"
        "sys.path.insert(0, 'scripts')\n"
        "import manage_users, memory\n"
        "def stub(cfg):\n"
        "    seen = (os.environ.get('ORT_DISABLE_TELEMETRY'),\n"
        "            os.environ.get('HF_HUB_DISABLE_TELEMETRY'), 'onnxruntime' in sys.modules)\n"
        "    if seen[0] == '1':\n"
        "        import mem0  # imports fastembed -> onnxruntime\n"
        "    return seen + (os.path.exists(os.path.expanduser('~/.cache/Microsoft')),)\n"
        "memory.build_memory = stub\n"
        "print(*manage_users._default_memory_factory())\n"
    )
    r = subprocess.run(
        [sys.executable, "-c", code],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    assert r.returncode == 0, r.stderr
    assert r.stdout.split() == ["1", "1", "False", "False"]
