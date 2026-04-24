"""Agent configuration.

Sources (in decreasing priority):
  1. env vars (TMASTER_AGENT_*)
  2. TOML file at path given by TMASTER_AGENT_CONFIG or default paths
  3. Pydantic defaults
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from pydantic_settings import BaseSettings, SettingsConfigDict


def _default_state_dir() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "tmaster"


def _default_runtime_dir() -> Path:
    base = os.environ.get("XDG_RUNTIME_DIR")
    if base:
        return Path(base) / "tmaster"
    return Path(f"/tmp/tmaster-{os.getuid()}")


class AgentSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="TMASTER_AGENT_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Connection
    server_url: str = "ws://127.0.0.1:8000"  # base, /ws/agent appended
    agent_id: Optional[str] = None
    agent_token: Optional[str] = None
    # Tls verification is controlled by the URL scheme (ws vs wss) and by
    # AGENT_TLS_INSECURE for local development.
    tls_insecure: bool = False

    # Agent identity
    machine_name: Optional[str] = None  # defaults to hostname

    # Runtime
    state_dir: Path = _default_state_dir()
    runtime_dir: Path = _default_runtime_dir()
    tmux_bin: str = "tmux"
    sidecar_bin: Optional[str] = None  # defaults to `tmaster-sidecar` in PATH

    # Supervision
    sidecar_restart_backoff_s: float = 1.0
    sidecar_max_consecutive_failures: int = 5

    # Workspace defaults
    default_workspace_cwd: Path = Path.home()
    session_prefix: str = "tm_"

    # Heartbeat
    heartbeat_interval_s: float = 15.0
    reconnect_initial_backoff_s: float = 1.0
    reconnect_max_backoff_s: float = 30.0

    def resolve_sidecar_bin(self) -> str:
        if self.sidecar_bin:
            return self.sidecar_bin
        # In a dev checkout, python -m tmaster.sidecar works without needing
        # to install the console script to PATH.
        return "tmaster-sidecar"


def load() -> AgentSettings:
    return AgentSettings()
