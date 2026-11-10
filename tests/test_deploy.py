"""The shipped Docker setup keeps Qdrant behind a key, telemetry off and the
models baked into the image.

Findings of the first real-stack smoke test: an unpublished Qdrant is still
reachable from the Docker host on its bridge address, so "not published" did
not protect it; Qdrant reported telemetry by default; and Mem0's BM25 encoder
was downloaded from Hugging Face on first use, after every container recreate.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
COMPOSE = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
DOCKERFILE = (ROOT / "Dockerfile").read_text(encoding="utf-8")
ENV_EXAMPLE = (ROOT / ".env.example").read_text(encoding="utf-8")

REQUIRED_KEY = re.compile(r"^\$\{QDRANT_API_KEY:\?[^}]*\.env[^}]*\}$")


def _env(service: str) -> dict[str, str]:
    env = COMPOSE["services"][service].get("environment", {})
    if isinstance(env, list):
        env = dict(item.split("=", 1) for item in env)
    return {k: str(v) for k, v in env.items()}


def test_bundled_qdrant_requires_an_api_key_and_compose_refuses_without_one():
    assert REQUIRED_KEY.match(_env("qdrant").get("QDRANT__SERVICE__API_KEY", ""))


def test_memsilo_gets_the_same_qdrant_key():
    assert REQUIRED_KEY.match(_env("memsilo").get("QDRANT_API_KEY", ""))


def test_qdrant_telemetry_is_disabled():
    assert _env("qdrant").get("QDRANT__TELEMETRY_DISABLED") == "true"


def test_qdrant_is_not_published():
    assert "ports" not in COMPOSE["services"]["qdrant"]


def test_env_example_requires_the_qdrant_key():
    assert re.search(r"^QDRANT_API_KEY=$", ENV_EXAMPLE, re.MULTILINE)
    assert "openssl rand -hex 32" in ENV_EXAMPLE
    assert "secrets.token_hex(32)" in ENV_EXAMPLE


def _env_lines() -> str:
    """Every ENV instruction, backslash continuations joined."""
    logical = DOCKERFILE.replace("\\\n", " ").splitlines()
    return " ".join(ln for ln in logical if ln.startswith("ENV "))


def test_image_switches_library_telemetry_off_and_hub_offline():
    env = _env_lines()
    for setting in ("ORT_DISABLE_TELEMETRY=1", "HF_HUB_DISABLE_TELEMETRY=1", "HF_HUB_OFFLINE=1"):
        assert setting in env, setting


def test_image_bakes_in_the_dense_and_bm25_models_and_fails_without_them():
    run = next(ln for ln in DOCKERFILE.splitlines() if "TextEmbedding(" in ln)
    assert "TextEmbedding('BAAI/bge-small-en-v1.5')" in run
    assert "SparseTextEmbedding('Qdrant/bm25')" in run
    # The runtime is offline (HF_HUB_OFFLINE=1): a build that could not fetch
    # the models must fail, not ship an image whose search silently degrades.
    assert "|| true" not in run


def test_offline_switch_comes_after_the_model_download():
    lines = DOCKERFILE.splitlines()
    fetch = next(i for i, ln in enumerate(lines) if "SparseTextEmbedding(" in ln)
    offline = next(i for i, ln in enumerate(lines) if "HF_HUB_OFFLINE=1" in ln)
    telemetry = next(i for i, ln in enumerate(lines) if "ORT_DISABLE_TELEMETRY=1" in ln)
    assert telemetry < fetch < offline
