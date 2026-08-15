from __future__ import annotations

import asyncio
import contextlib
import logging
import signal

from krauken.config import Config
from krauken.platforms.simulator.service import build_service


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    cfg = Config.from_env()
    cfg.simulator_socket.parent.mkdir(parents=True, exist_ok=True)
    service = build_service(socket_path=cfg.simulator_socket)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    stop_event = asyncio.Event()

    def _handle_signal() -> None:
        stop_event.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, _handle_signal)

    async def _run() -> None:
        await service.start()
        await stop_event.wait()
        await service.stop()

    loop.run_until_complete(_run())


if __name__ == "__main__":
    main()
