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
# Loopback-only by default -- deliberately conservative even though this is
# a zero-auth LAN appliance by design (see api/security.py's module
# docstring): a fresh install shouldn't be reachable from the network until
# someone explicitly opts in via KRAUKEN_API_HOST (e.g. "0.0.0.0" to reach
# it from a phone on the same LAN).
DEFAULT_API_HOST = "127.0.0.1"
DEFAULT_API_PORT = 8080


@dataclass(frozen=True, slots=True)
class Config:
    db_path: Path
    daemon_socket: Path
    supervisor_socket: Path
    heartbeat_interval_s: float = 60.0
    control_tick_interval_s: float = 30.0
    dev_panel_enabled: bool = False
    api_host: str = DEFAULT_API_HOST
    api_port: int = DEFAULT_API_PORT

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            db_path=Path(os.environ.get("KRAUKEN_DB_PATH", DEFAULT_DB_PATH)),
            daemon_socket=Path(os.environ.get("KRAUKEN_DAEMON_SOCKET", DEFAULT_DAEMON_SOCKET)),
            supervisor_socket=Path(os.environ.get("KRAUKEN_SUPERVISOR_SOCKET", DEFAULT_SUPERVISOR_SOCKET)),
            heartbeat_interval_s=float(os.environ.get("KRAUKEN_HEARTBEAT_INTERVAL_S", "60")),
            control_tick_interval_s=float(os.environ.get("KRAUKEN_CONTROL_TICK_INTERVAL_S", "30")),
            dev_panel_enabled=os.environ.get("KRAUKEN_DEV_PANEL", "0") == "1",
            api_host=os.environ.get("KRAUKEN_API_HOST", DEFAULT_API_HOST),
            api_port=int(os.environ.get("KRAUKEN_API_PORT", str(DEFAULT_API_PORT))),
        )
