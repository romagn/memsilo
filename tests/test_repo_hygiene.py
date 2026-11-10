"""Repository hygiene: ignore rules and test configuration."""

import ast
import subprocess
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.parametrize(
    "path",
    [
        "data/users.json",
        "data/users.json.lock",
        "data/history.db",
        "data/history.db-journal",
        "data/history.db-wal",
        "data/history.db-shm",
        "data/.users-abc.tmp",
        "data/anything-else",
    ],
)
def test_everything_in_data_is_ignored(path):
    """SQLite sidecars (-journal, -wal, -shm) hold customer text too."""
    r = subprocess.run(["git", "check-ignore", "-q", path], cwd=ROOT, check=False)
    assert r.returncode == 0, f"{path} is not git-ignored"


def test_data_gitkeep_is_not_ignored():
    r = subprocess.run(["git", "check-ignore", "-q", "data/.gitkeep"], cwd=ROOT, check=False)
    assert r.returncode == 1


def test_pytest_filters_only_the_known_starlette_deprecation():
    opts = tomllib.loads((ROOT / "pyproject.toml").read_text())["tool"]["pytest"]["ini_options"]
    filters = opts.get("filterwarnings", [])
    assert filters == ["ignore:The anyio.abc.BlockingPortal alias is deprecated:DeprecationWarning"]


def _sql_calls(path: Path):
    """Every `<x>.execute/executemany/executescript(first, ...)` call in `path`."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in ("execute", "executemany", "executescript")
            and node.args
        ):
            yield node.lineno, node.args[0]


def _tracked_py(*, shipped_only: bool) -> list[Path]:
    out = subprocess.run(
        ["git", "ls-files", "*.py"], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout.split()
    return [ROOT / p for p in out if not (shipped_only and p.startswith("tests/"))]


def test_shipped_sql_is_constant():
    """Shipped code passes only plain string literals as SQL; values go in bound
    parameters (lists of ids as one JSON parameter via json_each)."""
    bad = [
        f"{p.relative_to(ROOT)}:{line}"
        for p in _tracked_py(shipped_only=True)
        for line, arg in _sql_calls(p)
        if not (isinstance(arg, ast.Constant) and isinstance(arg.value, str))
    ]
    assert not bad, f"non-constant SQL: {bad}"


def test_no_formatted_sql_anywhere():
    """Not even tests build SQL with f-strings, `+`, `%` or .format()."""
    bad = [
        f"{p.relative_to(ROOT)}:{line}"
        for p in _tracked_py(shipped_only=False)
        for line, arg in _sql_calls(p)
        if isinstance(arg, (ast.JoinedStr, ast.BinOp))
        or (isinstance(arg, ast.Call) and getattr(arg.func, "attr", "") == "format")
    ]
    assert not bad, f"formatted SQL: {bad}"


def test_no_nosemgrep_suppressions():
    hits = [
        f"{p.relative_to(ROOT)}:{n}"
        for p in _tracked_py(shipped_only=False)
        for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1)
        if "nosemgrep" in line and p.name != "test_repo_hygiene.py"
    ]
    assert not hits, hits
