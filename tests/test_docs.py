"""Guard: the documentation matches the code it describes.

Every configuration key and environment variable the code reads is documented,
the docs name nothing the code does not have, and every relative link and
referenced file exists.
"""

import re
from pathlib import Path

import pytest
import yaml

import config as config_module

ROOT = Path(__file__).resolve().parent.parent
DOC_FILES = [ROOT / "README.md", *sorted((ROOT / "docs").glob("*.md"))]
DOC_TEXT = {p: p.read_text(encoding="utf-8") for p in DOC_FILES}
ALL_DOCS = "\n".join(DOC_TEXT.values())

SECRET_VARS = {"ANTHROPIC_API_KEY", "QDRANT_API_KEY"}
# Settings of the Qdrant server image (QDRANT__<section>__<key>), set on the
# compose qdrant service; memsilo itself does not read them.
QDRANT_SERVER_VARS = {"QDRANT__SERVICE__API_KEY", "QDRANT__TELEMETRY_DISABLED"}
# Read by docker compose (the image to run), not by memsilo itself.
COMPOSE_VARS = {"MEMSILO_IMAGE"}
# Files an operator creates or the server writes at runtime; docs may name them.
RUNTIME_FILES = {
    "config.yaml",
    "users.json",
    "history.db",
    "users.csv",
    "data/users.json",
    "data/history.db",
    "/app/config.yaml",
}


def _flatten(prefix: str, node) -> list[str]:
    if isinstance(node, dict):
        return [k for key, v in node.items() for k in _flatten(f"{prefix}{key}.", v)]
    return [prefix.rstrip(".")]


def example_keys() -> list[str]:
    data = yaml.safe_load((ROOT / "config.example.yaml").read_text(encoding="utf-8"))
    return _flatten("", data)


def code_env_vars() -> set[str]:
    """Every environment variable config.py reads."""
    from_fields = {env for (_s, _k, env, _t) in config_module._FIELDS.values()}
    source = (ROOT / "config.py").read_text(encoding="utf-8")
    from_source = set(re.findall(r'"((?:MEMSILO|ANTHROPIC|QDRANT)_[A-Z_]+)"', source))
    return from_fields | from_source | SECRET_VARS


def _words(text: str) -> set[str]:
    return set(re.findall(r"[A-Za-z0-9_.-]+", text))


# Env vars config.py reads that are not fields of Config, with their documented default.
OTHER_ENV_DEFAULTS = {
    "MEMSILO_CONFIG": "config.yaml",
    "MEMSILO_DOTENV": "./.env",
    "ANTHROPIC_API_KEY": "none",
    "QDRANT_API_KEY": "none",
}
# Pages that must carry the full configuration reference table.
TABLE_PAGES = [ROOT / "README.md", ROOT / "docs" / "index.md"]


def _cell(text: str) -> str:
    text = text.strip()
    match = re.fullmatch(r"`([^`]*)`", text)
    return match.group(1) if match else text


def _default_text(value) -> str:
    return str(value).lower() if isinstance(value, bool) else str(value)


def expected_rows() -> set[tuple[str, str, str]]:
    """(yaml key, env var, default) rows config.py defines."""
    defaults = config_module.Config()
    rows = set()
    for name, (section, key, env, _t) in config_module._FIELDS.items():
        yaml_key = (
            "(env only)"
            if name in config_module._ENV_ONLY
            else (f"{section}.{key}" if section else key)
        )
        rows.add((yaml_key, env, _default_text(getattr(defaults, name))))
    for env, default in OTHER_ENV_DEFAULTS.items():
        kind = "(secret)" if env in SECRET_VARS else "(env only)"
        rows.add((kind, env, default))
    return rows


def documented_rows(page: Path) -> list[tuple[str, str, str]]:
    lines = DOC_TEXT[page].splitlines()
    start = next(i for i, ln in enumerate(lines) if ln.startswith("| Key | Environment variable"))
    rows = []
    for line in lines[start + 2 :]:
        if not line.startswith("|"):
            break
        cells = [c for c in line.strip().strip("|").split("|")]
        rows.append((_cell(cells[0]), _cell(cells[1]), _cell(cells[2])))
    return rows


def test_expected_rows_cover_every_env_var_config_reads():
    assert {env for _k, env, _d in expected_rows()} == code_env_vars()


def test_example_file_documents_the_code_defaults():
    data = yaml.safe_load((ROOT / "config.example.yaml").read_text(encoding="utf-8"))
    defaults = config_module.Config()
    for name, (section, key, _e, _t) in config_module._FIELDS.items():
        if name in config_module._ENV_ONLY:
            continue
        value = data[section][key] if section else data[key]
        assert value == getattr(defaults, name), f"config.example.yaml {section}.{key}"


@pytest.mark.parametrize("page", TABLE_PAGES, ids=lambda p: p.name)
def test_configuration_table_matches_the_code(page):
    documented = documented_rows(page)
    assert len(documented) == len(set(documented)), f"{page.name}: duplicate rows"
    expected = expected_rows()
    got = set(documented)
    assert got == expected, (
        f"{page.name}: missing rows {sorted(expected - got)}; "
        f"rows the code does not define or with a wrong default {sorted(got - expected)}"
    )


def test_docs_name_no_unknown_env_var():
    # MEM0_TELEMETRY is set by mcp_server.py, not read from config.
    known = code_env_vars() | {"MEM0_TELEMETRY"} | QDRANT_SERVER_VARS | COMPOSE_VARS
    named = set(re.findall(r"\b(?:MEMSILO|ANTHROPIC|QDRANT|MEM0)_[A-Z0-9_]+\b", ALL_DOCS))
    assert not (named - known), f"docs name env vars the code lacks: {sorted(named - known)}"


def test_docs_name_no_unknown_config_key():
    keys = set(example_keys())
    sections = {k.split(".")[0] for k in keys if "." in k}
    pattern = re.compile(r"`((?:" + "|".join(sorted(sections)) + r")\.[a-z_]+)`")
    named = set(pattern.findall(ALL_DOCS))
    assert not (named - keys), f"docs name config keys the code lacks: {sorted(named - keys)}"


def test_compose_and_env_example_use_only_known_variables():
    known = code_env_vars() | QDRANT_SERVER_VARS | COMPOSE_VARS
    for name in ("docker-compose.yml", ".env.example"):
        text = (ROOT / name).read_text(encoding="utf-8")
        named = set(re.findall(r"\b(?:MEMSILO|ANTHROPIC|QDRANT)_[A-Z0-9_]+\b", text))
        assert not (named - known), f"{name} names unknown variables: {sorted(named - known)}"


def test_env_example_holds_only_secrets():
    text = (ROOT / ".env.example").read_text(encoding="utf-8")
    names = set(re.findall(r"^#?\s*([A-Z][A-Z0-9_]+)=", text, re.MULTILINE))
    assert names == SECRET_VARS


def test_relative_links_resolve():
    broken = []
    for path, text in DOC_TEXT.items():
        for target in re.findall(r"\]\(([^)\s]+)\)", text):
            if re.match(r"^[a-z][a-z0-9+.-]*:", target) or target.startswith("#"):
                continue
            rel = target.split("#")[0]
            if rel and not (path.parent / rel).exists():
                broken.append(f"{path.relative_to(ROOT)} -> {target}")
    assert not broken, f"broken relative links: {broken}"


def test_referenced_repo_files_exist():
    missing = []
    for path, text in DOC_TEXT.items():
        for ref in re.findall(r"`([A-Za-z0-9_./-]+\.(?:py|yaml|yml|md|toml|example))`", text):
            if ref in RUNTIME_FILES:
                continue
            candidates = [ROOT / ref, path.parent / ref, ROOT / "docs" / ref]
            if not any(c.exists() for c in candidates):
                missing.append(f"{path.relative_to(ROOT)}: {ref}")
    assert not missing, f"docs reference files that do not exist: {missing}"


def test_mkdocs_nav_files_exist():
    nav = yaml.safe_load((ROOT / "mkdocs.yml").read_text(encoding="utf-8"))["nav"]
    files = [next(iter(item.values())) for item in nav]
    missing = [f for f in files if not (ROOT / "docs" / f).exists()]
    assert not missing, f"mkdocs nav points at missing files: {missing}"
    documented = {p.name for p in (ROOT / "docs").glob("*.md")}
    assert documented == set(files), "every docs page must be in the mkdocs nav"


def test_readme_says_what_memsilo_adds_and_index_points_there():
    readme = DOC_TEXT[ROOT / "README.md"]
    assert "\n## What memsilo adds to Mem0, Qdrant and mcp\n" in readme
    assert "\n### What memsilo does not add\n" in readme
    assert "#what-memsilo-adds-to-mem0-qdrant-and-mcp" in DOC_TEXT[ROOT / "docs" / "index.md"]
