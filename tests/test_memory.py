import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from config import Config, ConfigError
from memory import build_memory, verify_collection_dims
from tests.fakes import FakeMemory, FakeQdrant

REPO = Path(__file__).resolve().parent.parent


def test_dims_mismatch_refuses():
    client = FakeQdrant.with_size("memsilo", 768)
    with pytest.raises(ConfigError) as exc:
        verify_collection_dims(client, "memsilo", 384)
    msg = str(exc.value)
    assert "768" in msg and "384" in msg
    assert "collection" in msg and "remove" in msg


def test_missing_collection_is_ok():
    verify_collection_dims(FakeQdrant(), "memsilo", 384)


def test_matching_dims_is_ok():
    verify_collection_dims(FakeQdrant.with_size("memsilo", 384), "memsilo", 384)


def test_named_vectors_dict_is_checked():
    named = FakeQdrant({"c": {"dense": SimpleNamespace(size=768)}})
    with pytest.raises(ConfigError, match="768"):
        verify_collection_dims(named, "c", 384)
    verify_collection_dims(FakeQdrant({"c": {"dense": SimpleNamespace(size=384)}}), "c", 384)


def test_unrecognised_vectors_shape_raises():
    with pytest.raises(ConfigError):
        verify_collection_dims(FakeQdrant({"c": None}), "c", 384)
    with pytest.raises(ConfigError):
        verify_collection_dims(FakeQdrant({"c": {}}), "c", 384)


def test_fake_memory_honours_user_filters():
    m = FakeMemory()
    m.add("a", user_id="alice", metadata=None)
    m.add("b", user_id="bob", metadata=None)
    assert [r["memory"] for r in m.get_all(filters={"user_id": "alice"}, top_k=5)["results"]] == [
        "a"
    ]
    with pytest.raises(ValueError):
        m.delete_all()
    m.delete_all(user_id="alice")
    assert m.get_all(filters={"user_id": "alice"}, top_k=5)["results"] == []


def _build(monkeypatch, **overrides):
    seen = {}

    class _Memory:
        @staticmethod
        def from_config(cfg):
            seen.update(cfg)
            return "mem"

    monkeypatch.setitem(sys.modules, "mem0", SimpleNamespace(Memory=_Memory))
    cfg = Config(**{"llm_api_key": "k", **overrides})
    assert build_memory(cfg) == "mem"
    return seen


def test_build_memory_uses_config_values(monkeypatch):
    seen = _build(
        monkeypatch,
        qdrant_url="https://q.example:6333",
        qdrant_api_key="qk",
        collection="mycol",
        embedder_dims=768,
        embedder_model="some/model",
        llm_model="llm-x",
    )
    vs = seen["vector_store"]["config"]
    assert vs["url"] == "https://q.example:6333" and vs["api_key"] == "qk"
    assert "host" not in vs and "port" not in vs
    assert vs["collection_name"] == "mycol" and vs["embedding_model_dims"] == 768
    assert seen["llm"]["provider"] == "anthropic"
    assert seen["llm"]["config"] == {"model": "llm-x", "api_key": "k"}
    assert seen["embedder"] == {"provider": "fastembed", "config": {"model": "some/model"}}


@pytest.mark.parametrize("api_key", ["", "qk"])
def test_build_memory_qdrant_section_passes_mem0_validation(monkeypatch, api_key):
    """Mem0's QdrantConfig rejects url without api_key unless path is also given."""
    from mem0.configs.vector_stores.qdrant import QdrantConfig

    vs = _build(monkeypatch, qdrant_api_key=api_key)["vector_store"]["config"]
    QdrantConfig(**vs)


def _child(extra_env):
    env = {
        "PATH": os.environ["PATH"],
        "ANTHROPIC_API_KEY": "dummy",
        "PYTHONPATH": str(REPO),
        "MEMSILO_QDRANT_URL": "http://127.0.0.1:1",
        "MEMSILO_DOTENV": "",
    }
    env.update(extra_env)
    code = (
        "import os, sys, tempfile\n"
        "os.chdir(tempfile.mkdtemp())\n"
        "import mcp_server\n"
        "print(os.environ.get('MEM0_TELEMETRY'), 'mem0' in sys.modules)\n"
    )
    r = subprocess.run(
        [sys.executable, "-c", code],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert r.returncode == 0, r.stderr
    return r.stdout.split()


def test_telemetry_is_off_by_default():
    telemetry, mem0_loaded = _child({})
    assert telemetry == "false"
    assert mem0_loaded == "False"


def test_telemetry_can_be_enabled():
    assert _child({"MEMSILO_TELEMETRY": "true"})[0] == "true"


# --- onnxruntime / Hugging Face telemetry ------------------------------------
#
# Importing mem0 imports fastembed and with it onnxruntime 1.29, whose native
# library starts Microsoft's telemetry at import: it creates
# ~/.cache/Microsoft/DeveloperTools/.onnxruntime/{deviceid,onnxruntime.db} and
# queues events for upload. It reads ORT_DISABLE_TELEMETRY at that moment;
# calling onnxruntime.disable_telemetry_events() afterwards still leaves the
# import-time events on disk. So the variable must be set before mem0 is first
# imported, by the server itself.

_ORT_CHILD = (
    "import os, sys, tempfile\n"
    "os.chdir(tempfile.mkdtemp())\n"
    "import mcp_server\n"
    "print(os.environ.get('ORT_DISABLE_TELEMETRY'), os.environ.get('HF_HUB_DISABLE_TELEMETRY'),"
    " 'onnxruntime' in sys.modules)\n"
    # Only import the library (which would start its telemetry) once the
    # switch is known to be set, so a regression fails without phoning home.
    "if os.environ.get('ORT_DISABLE_TELEMETRY') == '1':\n"
    "    import mem0  # imports fastembed -> onnxruntime\n"
    "    assert 'onnxruntime' in sys.modules\n"
    "    d = os.path.expanduser('~/.cache/Microsoft')\n"
    "    print(os.path.exists(d))\n"
)


def _ort_child(tmp_path, extra_env=None):
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(tmp_path),
        "ANTHROPIC_API_KEY": "dummy",
        "PYTHONPATH": str(REPO),
        "MEMSILO_QDRANT_URL": "http://127.0.0.1:1",
        "MEMSILO_DOTENV": "",
        **(extra_env or {}),
    }
    r = subprocess.run(
        [sys.executable, "-c", _ORT_CHILD],
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    assert r.returncode == 0, r.stderr
    return r.stdout.split()


def test_server_disables_onnxruntime_telemetry_before_it_is_imported(tmp_path):
    ort, hf, loaded, *rest = _ort_child(tmp_path)
    assert ort == "1"
    assert hf == "1"
    assert loaded == "False", "onnxruntime must not be imported before the switch is set"
    assert rest == ["False"], "onnxruntime wrote its telemetry files anyway"


def test_server_overrides_an_inherited_onnxruntime_telemetry_setting(tmp_path):
    assert _ort_child(tmp_path, {"ORT_DISABLE_TELEMETRY": "0"})[0] == "1"
