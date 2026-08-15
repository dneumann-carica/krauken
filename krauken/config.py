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
DEFAULT_SIMULATOR_SOCKET = "/run/krauken/simulator.sock"
DEFAULT_MANUAL_SOCKET = "/run/krauken/manual.sock"
# LAN-reachable by default -- the Pi this runs on is headless, so a
# loopback-only default would leave a fresh install unreachable from
# anywhere until someone already knew to SSH in and edit a config file.
# api/security.py's CSRF-style mitigation (a required custom header on
# every mutating request, enforced alongside a same-origin CORS policy)
# was already built assuming LAN reachability -- "any hostile webpage a
# browser on that network visits" -- so this isn't removing a security
# layer, just matching the binding to what that layer already defends
# against. Set KRAUKEN_API_HOST=127.0.0.1 to lock a given install back
# down to loopback-only.
DEFAULT_API_HOST = "0.0.0.0"
DEFAULT_API_PORT = 8080
# No KRAUKEN_TILT_HCI_DEVICE here -- that's Tilt-hardware-specific
# configuration, and lives entirely inside platforms/tilt/scanner.py's own
# TiltScanner now (reads the env var itself), not threaded through Config/
# the daemon composition root. Same reasoning as simulator_socket/
# manual_socket below NOT including a "the daemon needs this" comment
# anymore -- see platforms/registry.py's PlatformRegistry, which is the
# only thing that ever constructs a TiltScanner/BrewPiConnection/IPC
# connection now. No color config at all for Tilt either -- project
# decision (explicit user direction): the Tilt scanner watches for all 8
# known colors unconditionally and discover() surfaces whichever are
# actually detected as candidates, the same "scan and see what's really
# there" philosophy as BrewPi's auto-scan, rather than requiring the color
# be known/configured up front.
# No DEFAULT_BREWPI_* either, for the identical reason -- auto-scan every
# /dev/ttyACM*/ttyUSB* rather than a configured port (see
# platforms/brewpi/platform.py), since a single-chamber install has at
# most one BrewPi to find.


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
    # Simulator/Manual's own sockets -- the API talks to these DIRECTLY for
    # dev-panel ops (api/routers/dev_panel.py), never through the daemon.
    # The daemon no longer reads these fields at all -- platforms/
    # ipc_driver.py's ManualIpcConnection/SimulatorIpcConnection read the
    # same KRAUKEN_MANUAL_SOCKET/KRAUKEN_SIMULATOR_SOCKET env vars
    # themselves (platforms/registry.py's PlatformRegistry is what
    # constructs them, never the daemon composition root). These fields
    # stay here because platforms/simulator/__main__.py and platforms/
    # manual/__main__.py's own service processes still need to know their
    # OWN bind path. Same "one socket per out-of-process platform" shape
    # supervisor_socket already established, just for the two mock
    # platforms instead of real hardware.
    simulator_socket: Path = Path(DEFAULT_SIMULATOR_SOCKET)
    manual_socket: Path = Path(DEFAULT_MANUAL_SOCKET)

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
            simulator_socket=Path(os.environ.get("KRAUKEN_SIMULATOR_SOCKET", DEFAULT_SIMULATOR_SOCKET)),
            manual_socket=Path(os.environ.get("KRAUKEN_MANUAL_SOCKET", DEFAULT_MANUAL_SOCKET)),
        )
