import os
import sys
from dataclasses import replace
from pathlib import Path

import pytest

# Mem0 reads this once at import; a test importing mem0 alone must not default to telemetry on.
os.environ.setdefault("MEM0_TELEMETRY", "false")
# Importing mem0 imports onnxruntime, which starts Microsoft telemetry at import
# unless this is set first (see memory.telemetry_env). Tests that check the
# server and CLI set it themselves run in child processes with their own env.
os.environ.setdefault("ORT_DISABLE_TELEMETRY", "1")
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")

# A developer's .env must never influence the test process (config reads it at import).
os.environ["MEMSILO_DOTENV"] = ""

# Add project root to Python path so imports work
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))


@pytest.fixture(autouse=True)
def _private_history_db(tmp_path, monkeypatch):
    """delete_memory purges config.history_db; never let a test touch a real
    data/history.db in the working directory."""
    server = sys.modules.get("mcp_server")
    if server is not None:
        monkeypatch.setattr(
            server, "config", replace(server.config, history_db=str(tmp_path / "history.db"))
        )
