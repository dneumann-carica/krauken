from __future__ import annotations

import asyncio
import contextlib
import logging
import signal

from krauken.config import Config
from krauken.daemon.app import build_daemon


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    cfg = Config.from_env()
    cfg.db_path.parent.mkdir(parents=True, exist_ok=True)
    daemon = build_daemon(
        db_path=cfg.db_path,
        socket_path=cfg.daemon_socket,
        heartbeat_interval_s=cfg.heartbeat_interval_s,
        control_tick_interval_s=cfg.control_tick_interval_s,
        # clock left unset -- build_daemon()'s _select_clock() picks
        # SimulatorClock/ProductionClock based on what's actually mapped in
        # hardware_config, independent of KRAUKEN_DEV_PANEL (see its
        # docstring in daemon/app.py).
    )

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    stop_event = asyncio.Event()

    def _handle_signal() -> None:
        stop_event.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, _handle_signal)

    async def _run() -> None:
        await daemon.start()
        await stop_event.wait()
        await daemon.stop()

    loop.run_until_complete(_run())


if __name__ == "__main__":
    main()
