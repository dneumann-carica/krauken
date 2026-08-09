"""Config loading: environment variables (highest precedence, used for dev/
CI) over /etc/krauken/krauken.toml (production) over these defaults. Kept
deliberately tiny for M0 -- grows as later milestones need more settings.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_DB_PATH = "/var/lib/krauken/krauken.db"
DEFAULT_DAEMON_SOCKET = "/run/krauken/daemon.sock"
DEFAULT_SUPERVISOR_SOCKET = "/run/krauken/supervisor.sock"


@dataclass(frozen=True, slots=True)
class Config:
    db_path: Path
    daemon_socket: Path
    supervisor_socket: Path
    heartbeat_interval_s: float = 60.0
    control_tick_interval_s: float = 30.0
    dev_panel_enabled: bool = False

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            db_path=Path(os.environ.get("KRAUKEN_DB_PATH", DEFAULT_DB_PATH)),
            daemon_socket=Path(os.environ.get("KRAUKEN_DAEMON_SOCKET", DEFAULT_DAEMON_SOCKET)),
            supervisor_socket=Path(os.environ.get("KRAUKEN_SUPERVISOR_SOCKET", DEFAULT_SUPERVISOR_SOCKET)),
            heartbeat_interval_s=float(os.environ.get("KRAUKEN_HEARTBEAT_INTERVAL_S", "60")),
            control_tick_interval_s=float(os.environ.get("KRAUKEN_CONTROL_TICK_INTERVAL_S", "30")),
            dev_panel_enabled=os.environ.get("KRAUKEN_DEV_PANEL", "0") == "1",
        )
