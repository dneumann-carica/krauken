"""IPC ops for hardware discovery and role mapping. Imported by
krauken.daemon.app for its @op-decorated side effects -- see
krauken.ipc.server.OPS.
"""
from __future__ import annotations

from typing import Any, Mapping

from krauken.daemon import hardware, tests_runtime
from krauken.daemon.discovery import DiscoveryService
from krauken.ipc.server import op

_services: dict[int, DiscoveryService] = {}


def _discovery_for(ctx: Any) -> DiscoveryService:
    # One DiscoveryService per ctx (i.e. per daemon process/test instance),
    # memoized by ctx identity rather than stored on ctx itself, so
    # DaemonContext doesn't need to know about discovery at all.
    key = id(ctx)
    if key not in _services:
        _services[key] = DiscoveryService(ctx)
    return _services[key]


@op("hardware.scan_start", mutating=True)
async def _scan_start(ctx: Any, args: Mapping[str, Any]) -> dict[str, Any]:
    return _discovery_for(ctx).start_scan()


@op("hardware.scan_status")
async def _scan_status(ctx: Any, args: Mapping[str, Any]) -> dict[str, Any]:
    return _discovery_for(ctx).scan_status(args["scan_id"])


@op("hardware.mapping_save", mutating=True)
async def _mapping_save(ctx: Any, args: Mapping[str, Any]) -> dict[str, Any]:
    return await hardware.save_mapping(ctx, args["roles"])


@op("hardware.test_start", mutating=True)
async def _test_start(ctx: Any, args: Mapping[str, Any]) -> dict[str, Any]:
    return tests_runtime.start_test(ctx, args["device_id"], args["action"], args.get("params", {}))


@op("hardware.test_status")
async def _test_status(ctx: Any, args: Mapping[str, Any]) -> dict[str, Any]:
    return tests_runtime.test_status(ctx, args["test_id"])


@op("hardware.test_cancel", mutating=True)
async def _test_cancel(ctx: Any, args: Mapping[str, Any]) -> dict[str, Any]:
    return await tests_runtime.cancel_test(ctx, args["test_id"])
