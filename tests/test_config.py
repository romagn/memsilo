import os
import subprocess
import sys
from pathlib import Path

import pytest

import config as config_module
from config import Config, ConfigError, load_config

REPO_ROOT = Path(__file__).resolve().parent.parent
KEY = {"ANTHROPIC_API_KEY": "k"}


@pytest.fixture(autouse=True)
def _hermetic(tmp_path, monkeypatch):
    """No test may see a developer's ./config.yaml or MEMSILO_CONFIG."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("MEMSILO_CONFIG", raising=False)


def _write(tmp_path, text):
    p = tmp_path / "config.yaml"
    p.write_text(text)
    return str(p)


def test_defaults_without_file_or_env(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # no ./config.yaml here
    cfg = load_config(path=None, env=KEY)
    assert cfg == Config(llm_api_key="k")
    assert cfg.host == "127.0.0.1"
    assert cfg.port == 8100
    assert cfg.transport == "stdio"
    assert cfg.collection == "memsilo"
    assert cfg.embedder_dims == 384
    assert cfg.users_file == "data/users.json"
    assert cfg.history_db == "data/history.db"
    assert cfg.telemetry is False


def test_file_overrides_defaults_and_env_overrides_file(tmp_path):
    path = _write(tmp_path, "server:\n  port: 9000\nqdrant:\n  collection: c1\n")
    assert load_config(path=path, env=KEY).port == 9000
    cfg = load_config(path=path, env={**KEY, "MEMSILO_SERVER_PORT": "9100"})
    assert cfg.port == 9100
    assert cfg.collection == "c1"


def test_env_only_values_and_secrets(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = load_config(
        env={
            **KEY,
            "QDRANT_API_KEY": "q",
            "MEMSILO_TELEMETRY": "true",
            "MEMSILO_EMBEDDER_DIMS": "768",
            "MEMSILO_DEFAULT_USER_ID": "me",
        }
    )
    assert (cfg.llm_api_key, cfg.qdrant_api_key) == ("k", "q")
    assert cfg.telemetry is True
    assert cfg.embedder_dims == 768
    assert cfg.default_user_id == "me"


def test_memsilo_config_env_selects_file(tmp_path):
    path = _write(tmp_path, "qdrant:\n  collection: viaenv\n")
    assert load_config(env={**KEY, "MEMSILO_CONFIG": path}).collection == "viaenv"


def test_unknown_key_names_the_key(tmp_path):
    with pytest.raises(ConfigError, match="qdrnat"):
        load_config(path=_write(tmp_path, "qdrnat: {}\n"), env=KEY)


def test_unknown_nested_key_names_the_key(tmp_path):
    with pytest.raises(ConfigError, match="prot"):
        load_config(path=_write(tmp_path, "server:\n  prot: 1\n"), env=KEY)


def test_secret_in_file_is_rejected(tmp_path):
    with pytest.raises(ConfigError, match="environment"):
        load_config(path=_write(tmp_path, "llm:\n  api_key: x\n"), env=KEY)


def test_missing_llm_key_fails_fast():
    with pytest.raises(ConfigError, match="ANTHROPIC_API_KEY"):
        load_config(path=None, env={})


def test_missing_llm_key_tolerated_when_not_required(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = load_config(env={}, require_llm_key=False)
    assert cfg.llm_api_key == ""
    with pytest.raises(ConfigError, match="ANTHROPIC_API_KEY"):
        cfg.require_llm_key()
    Config(llm_api_key="k").require_llm_key()  # does not raise


def test_explicit_missing_path_errors(tmp_path):
    with pytest.raises(ConfigError, match="nope.yaml"):
        load_config(path=str(tmp_path / "nope.yaml"), env=KEY)


def test_bad_port_type_errors(tmp_path):
    with pytest.raises(ConfigError, match="port"):
        load_config(path=_write(tmp_path, 'server:\n  port: "abc"\n'), env=KEY)


def test_bad_port_in_env_errors():
    with pytest.raises(ConfigError, match="MEMSILO_SERVER_PORT"):
        load_config(env={**KEY, "MEMSILO_SERVER_PORT": "abc"})


def test_bad_transport_errors():
    with pytest.raises(ConfigError, match="transport"):
        load_config(env={**KEY, "MEMSILO_TRANSPORT": "carrier-pigeon"})


def test_example_file_loads():
    cfg = load_config(path=str(REPO_ROOT / "config.example.yaml"), env=KEY)
    assert cfg == Config(llm_api_key="k")


def test_user_cli_works_without_api_key(tmp_path):
    """Importing config and running the user CLI must not need an LLM key."""
    result = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts" / "manage_users.py"),
            "--users-file",
            str(tmp_path / "users.json"),
            "list",
        ],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        env={
            "PATH": "/usr/bin:/bin",
            "HOME": str(tmp_path),
            "MEMSILO_QDRANT_URL": "http://127.0.0.1:1",
            "MEMSILO_DOTENV": "",
        },
        check=False,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "body,key",
    [
        ("embedder:\n  dims: 0\n", "dims"),
        ("embedder:\n  dims: -5\n", "dims"),
        ("server:\n  port: 0\n", "port"),
        ("server:\n  port: -1\n", "port"),
        ("server:\n  port: 99999\n", "port"),
    ],
)
def test_out_of_range_numbers_in_file_error(tmp_path, body, key):
    with pytest.raises(ConfigError, match=key):
        load_config(path=_write(tmp_path, body), env=KEY)


@pytest.mark.parametrize(
    "var,value",
    [
        ("MEMSILO_EMBEDDER_DIMS", "0"),
        ("MEMSILO_EMBEDDER_DIMS", "-5"),
        ("MEMSILO_SERVER_PORT", "99999"),
        ("MEMSILO_SERVER_PORT", "0"),
    ],
)
def test_out_of_range_numbers_in_env_error(var, value):
    with pytest.raises(ConfigError, match=var):
        load_config(env={**KEY, var: value})


def test_range_boundaries_accepted():
    cfg = load_config(env={**KEY, "MEMSILO_SERVER_PORT": "65535", "MEMSILO_EMBEDDER_DIMS": "1"})
    assert (cfg.port, cfg.embedder_dims) == (65535, 1)


def test_history_db_from_file_and_env(tmp_path):
    path = _write(tmp_path, "storage:\n  history_db: /v/h.db\n")
    assert load_config(path=path, env=KEY).history_db == "/v/h.db"
    env = {**KEY, "MEMSILO_HISTORY_DB": "/e/h.db"}
    assert load_config(path=path, env=env).history_db == "/e/h.db"


def test_history_db_must_be_a_string(tmp_path):
    path = _write(tmp_path, "storage:\n  history_db: 5\n")
    with pytest.raises(ConfigError, match="history_db"):
        load_config(path=path, env=KEY)


# --- .env discovery: current directory only, never an ancestor ----------------


@pytest.fixture
def clean_key(monkeypatch):
    """load_dotenv writes into os.environ; make monkeypatch undo that afterwards."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "placeholder")
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    monkeypatch.delenv("MEMSILO_DOTENV", raising=False)


def test_dotenv_in_cwd_is_read(tmp_path, monkeypatch, clean_key):
    (tmp_path / ".env").write_text("ANTHROPIC_API_KEY=from-cwd\n")
    monkeypatch.chdir(tmp_path)
    config_module.load_dotenv_file()
    assert os.environ["ANTHROPIC_API_KEY"] == "from-cwd"


def test_dotenv_in_a_parent_directory_is_not_read(tmp_path, monkeypatch, clean_key):
    (tmp_path / ".env").write_text("ANTHROPIC_API_KEY=from-parent\n")
    child = tmp_path / "a" / "b"
    child.mkdir(parents=True)
    monkeypatch.chdir(child)
    config_module.load_dotenv_file()
    assert "ANTHROPIC_API_KEY" not in os.environ


def test_empty_memsilo_dotenv_disables_loading(tmp_path, monkeypatch, clean_key):
    (tmp_path / ".env").write_text("ANTHROPIC_API_KEY=from-cwd\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MEMSILO_DOTENV", "")
    config_module.load_dotenv_file()
    assert "ANTHROPIC_API_KEY" not in os.environ


def test_memsilo_dotenv_names_the_file(tmp_path, monkeypatch, clean_key):
    elsewhere = tmp_path / "secrets.env"
    elsewhere.write_text("ANTHROPIC_API_KEY=from-path\n")
    (tmp_path / ".env").write_text("ANTHROPIC_API_KEY=from-cwd\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MEMSILO_DOTENV", str(elsewhere))
    config_module.load_dotenv_file()
    assert os.environ["ANTHROPIC_API_KEY"] == "from-path"


def test_real_environment_beats_dotenv(tmp_path, monkeypatch, clean_key):
    (tmp_path / ".env").write_text("ANTHROPIC_API_KEY=from-file\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "from-env")
    config_module.load_dotenv_file()
    assert os.environ["ANTHROPIC_API_KEY"] == "from-env"


# --- empty strings fail closed (final fix wave A1) ---------------------------

# field -> (env var, yaml section, yaml key); default_user_id is env-only.
_STR_FIELDS = {
    "host": ("MEMSILO_SERVER_HOST", "server", "host"),
    "qdrant_url": ("MEMSILO_QDRANT_URL", "qdrant", "url"),
    "collection": ("MEMSILO_COLLECTION", "qdrant", "collection"),
    "users_file": ("MEMSILO_USERS_FILE", "storage", "users_file"),
    "history_db": ("MEMSILO_HISTORY_DB", "storage", "history_db"),
    "llm_model": ("MEMSILO_LLM_MODEL", "llm", "model"),
    "embedder_model": ("MEMSILO_EMBEDDER_MODEL", "embedder", "model"),
    "default_user_id": ("MEMSILO_DEFAULT_USER_ID", None, None),
}


@pytest.mark.parametrize("value", ["", "   ", "\t\n"])
@pytest.mark.parametrize("field", sorted(_STR_FIELDS))
def test_empty_string_from_env_is_rejected(field, value):
    var = _STR_FIELDS[field][0]
    with pytest.raises(ConfigError, match=var):
        load_config(env={**KEY, var: value})


@pytest.mark.parametrize("value", ['""', '"   "'])
@pytest.mark.parametrize("field", sorted(f for f, v in _STR_FIELDS.items() if v[1]))
def test_empty_string_in_yaml_is_rejected(tmp_path, field, value):
    _var, section, key = _STR_FIELDS[field]
    path = _write(tmp_path, f"{section}:\n  {key}: {value}\n")
    with pytest.raises(ConfigError, match=rf"{section}\.{key}"):
        load_config(path=path, env=KEY)


def test_empty_secrets_keep_their_meaning():
    # Empty QDRANT_API_KEY means "no key"; empty ANTHROPIC_API_KEY means missing.
    cfg = load_config(env={"ANTHROPIC_API_KEY": "k", "QDRANT_API_KEY": ""})
    assert cfg.qdrant_api_key == ""
    with pytest.raises(ConfigError, match="ANTHROPIC_API_KEY"):
        load_config(env={"ANTHROPIC_API_KEY": ""})


@pytest.mark.parametrize("bad", ["*", "a*", "%", "a b", "../x", "ünï", "-x", "x" * 65])
def test_default_user_id_must_be_a_valid_user_id(bad):
    with pytest.raises(ConfigError, match="MEMSILO_DEFAULT_USER_ID"):
        load_config(env={**KEY, "MEMSILO_DEFAULT_USER_ID": bad})


def test_default_default_user_id_passes():
    assert load_config(env=KEY).default_user_id == "default"
    assert load_config(env={**KEY, "MEMSILO_DEFAULT_USER_ID": "cust_01"}).default_user_id == (
        "cust_01"
    )
