"""Configuration for the memsilo MCP server.

Precedence, highest first: environment variable, then ``config.yaml``, then the
built-in default. Secrets (API keys) are read from the environment only; a
secret in the yaml file is an error. Unknown yaml keys and values of the wrong
type are errors that name the offending key.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any

import yaml
from dotenv import load_dotenv

from users import validate_user_id


def load_dotenv_file() -> None:
    """Load a ``.env`` file into the environment, without overriding real variables.

    Only ``./.env`` in the current working directory is read: never an ancestor
    directory (python-dotenv's default search would walk up to ``/``). If
    ``MEMSILO_DOTENV`` is set, that path is read instead, and an empty value
    disables loading altogether.
    """
    spec = os.environ.get("MEMSILO_DOTENV")
    if spec == "":
        return
    path = spec if spec is not None else os.path.join(os.getcwd(), ".env")
    if os.path.isfile(path):
        # override=False: a real environment variable always beats a value in `.env`.
        load_dotenv(path, override=False)


load_dotenv_file()

TRANSPORTS = ("stdio", "sse", "streamable-http")


class ConfigError(Exception):
    """The configuration is invalid or incomplete."""


@dataclass(frozen=True)
class Config:
    host: str = "127.0.0.1"
    port: int = 8100
    transport: str = "stdio"

    qdrant_url: str = "http://localhost:6333"
    qdrant_api_key: str = ""
    collection: str = "memsilo"

    llm_provider: str = "anthropic"
    llm_model: str = "claude-haiku-4-5-20251001"
    llm_api_key: str = ""

    embedder_provider: str = "fastembed"
    embedder_model: str = "BAAI/bge-small-en-v1.5"
    embedder_dims: int = 384

    # Per-user token store, and the user that stdio (local, unauthenticated)
    # launches act as.
    users_file: str = "data/users.json"
    default_user_id: str = "default"

    # Mem0's history database (SQLite). It keeps the text of every memory ever
    # stored, so per-user erase purges it too.
    history_db: str = "data/history.db"

    telemetry: bool = False

    def require_llm_key(self) -> None:
        """Raise ConfigError if the configured LLM provider needs a key and has none.

        Kept out of load_config's import-time path (see ``config`` below) so that
        importing this module, and the user-management CLI, work without a key;
        the server calls this at startup instead.
        """
        if self.llm_provider == "anthropic" and not self.llm_api_key:
            raise ConfigError(
                "ANTHROPIC_API_KEY is not set. Export it in the environment "
                "(or put it in .env); it is never read from config.yaml."
            )


# field name -> (yaml section, yaml key, env var, type)
_FIELDS: dict[str, tuple[str, str, str, type]] = {
    "host": ("server", "host", "MEMSILO_SERVER_HOST", str),
    "port": ("server", "port", "MEMSILO_SERVER_PORT", int),
    "transport": ("server", "transport", "MEMSILO_TRANSPORT", str),
    "qdrant_url": ("qdrant", "url", "MEMSILO_QDRANT_URL", str),
    "collection": ("qdrant", "collection", "MEMSILO_COLLECTION", str),
    "llm_provider": ("llm", "provider", "MEMSILO_LLM_PROVIDER", str),
    "llm_model": ("llm", "model", "MEMSILO_LLM_MODEL", str),
    "embedder_provider": ("embedder", "provider", "MEMSILO_EMBEDDER_PROVIDER", str),
    "embedder_model": ("embedder", "model", "MEMSILO_EMBEDDER_MODEL", str),
    "embedder_dims": ("embedder", "dims", "MEMSILO_EMBEDDER_DIMS", int),
    "users_file": ("storage", "users_file", "MEMSILO_USERS_FILE", str),
    "history_db": ("storage", "history_db", "MEMSILO_HISTORY_DB", str),
    "default_user_id": ("", "default_user_id", "MEMSILO_DEFAULT_USER_ID", str),
    "telemetry": ("", "telemetry", "MEMSILO_TELEMETRY", bool),
}
# `default_user_id` has no yaml home in the documented layout; it is env-only.
_ENV_ONLY = {"default_user_id"}

_SECRET_WORDS = ("api_key", "apikey", "secret", "token", "password")


_RANGES: dict[str, tuple[int, int | None]] = {"port": (1, 65535), "embedder_dims": (1, None)}


def _check_range(name: str, value: Any, where: str) -> None:
    if name not in _RANGES:
        return
    lo, hi = _RANGES[name]
    if value < lo or (hi is not None and value > hi):
        allowed = f">= {lo}" if hi is None else f"{lo}-{hi}"
        raise ConfigError(f"{where}: {value} is out of range (allowed: {allowed})")


def _sections() -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for name, (section, key, _env, _t) in _FIELDS.items():
        if section and name not in _ENV_ONLY:
            out.setdefault(section, set()).add(key)
    return out


def _coerce(value: Any, typ: type, where: str, *, from_env: bool) -> Any:
    if typ is bool:
        if isinstance(value, bool):
            return value
        if from_env and isinstance(value, str):
            low = value.strip().lower()
            if low in ("1", "true", "yes", "on"):
                return True
            if low in ("0", "false", "no", "off", ""):
                return False
        raise ConfigError(f"{where}: expected a boolean, got {value!r}")
    if typ is int:
        if isinstance(value, bool):
            raise ConfigError(f"{where}: expected an integer, got {value!r}")
        if isinstance(value, int):
            return value
        if from_env and isinstance(value, str):
            try:
                return int(value.strip())
            except ValueError:
                pass
        raise ConfigError(f"{where}: expected an integer, got {value!r}")
    if not isinstance(value, str):
        raise ConfigError(f"{where}: expected a string, got {value!r}")
    # Fail closed: an empty host would bind every interface (and skip mcp's
    # loopback DNS-rebinding protection), an empty history_db would make erase
    # skip the purge, an empty users_file would refuse every token.
    if not value.strip():
        raise ConfigError(f"{where}: must not be empty")
    return value


def _read_file(path: str, explicit: bool) -> dict[str, Any]:
    if not os.path.isfile(path):
        if explicit:
            raise ConfigError(f"config file not found: {path}")
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
    except yaml.YAMLError as e:
        raise ConfigError(f"{path}: invalid YAML: {e}") from e
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: top level must be a mapping")
    return data


def _file_values(data: dict[str, Any], path: str) -> dict[str, Any]:
    """Validate the parsed yaml and return {field name: value}."""
    sections = _sections()
    for section, body in data.items():
        if section not in sections and section != "telemetry":
            raise ConfigError(f"{path}: unknown key {section!r}")
        if section == "telemetry":
            continue
        if body is None:
            continue
        if not isinstance(body, dict):
            raise ConfigError(f"{path}: {section!r} must be a mapping")
        for key in body:
            if any(w in str(key).lower() for w in _SECRET_WORDS):
                raise ConfigError(
                    f"{path}: {section}.{key} looks like a secret; secrets are read "
                    "from the environment only (ANTHROPIC_API_KEY, QDRANT_API_KEY), "
                    "never from the config file"
                )
            if key not in sections[section]:
                raise ConfigError(f"{path}: unknown key {section}.{key}")

    values: dict[str, Any] = {}
    for name, (section, key, _env, typ) in _FIELDS.items():
        if name in _ENV_ONLY:
            continue
        if section == "":
            raw = data.get(key)
        else:
            body = data.get(section) or {}
            raw = body.get(key)
        if raw is None:
            continue
        where = f"{path}: {section + '.' if section else ''}{key}"
        values[name] = _coerce(raw, typ, where, from_env=False)
        _check_range(name, values[name], where)
    return values


def load_config(
    path: str | None = None,
    env: Mapping[str, str] | None = None,
    *,
    require_llm_key: bool = True,
) -> Config:
    """Build a Config from yaml file, environment and defaults (see module doc).

    With ``require_llm_key`` (the default) a missing ANTHROPIC_API_KEY is a
    ConfigError when the provider is ``anthropic``.
    """
    env = os.environ if env is None else env

    explicit = path is not None or bool(env.get("MEMSILO_CONFIG"))
    file_path = path or env.get("MEMSILO_CONFIG") or "config.yaml"
    values = _file_values(_read_file(file_path, explicit), file_path)

    for name, (_section, _key, var, typ) in _FIELDS.items():
        if var in env:
            values[name] = _coerce(env[var], typ, var, from_env=True)
            _check_range(name, values[name], var)

    if values.get("transport", Config.transport) not in TRANSPORTS:
        raise ConfigError(
            f"transport must be one of {', '.join(TRANSPORTS)}, got {values['transport']!r}"
        )

    if "default_user_id" in values:
        try:
            validate_user_id(values["default_user_id"])
        except ValueError as e:
            raise ConfigError(f"MEMSILO_DEFAULT_USER_ID: {e}") from None

    cfg = Config(**values)
    secrets: dict[str, str] = {}
    if env.get("QDRANT_API_KEY"):
        secrets["qdrant_api_key"] = env["QDRANT_API_KEY"]
    if cfg.llm_provider == "anthropic" and env.get("ANTHROPIC_API_KEY"):
        secrets["llm_api_key"] = env["ANTHROPIC_API_KEY"]
    cfg = replace(cfg, **secrets)
    if require_llm_key:
        cfg.require_llm_key()
    return cfg


# Importing this module must never fail for lack of an API key (tests and the
# user CLI import it); the server enforces the key at startup via
# `config.require_llm_key()`.
config = load_config(require_llm_key=False)
