"""IPC ops for the fermentation lifecycle. Imported by krauken.daemon.app
for its @op-decorated side effects -- see krauken.ipc.server.OPS.
"""
from __future__ import annotations

from typing import Any, Mapping

from krauken.daemon import fermentation
from krauken.ipc.server import op


@op("fermentation.start", mutating=True)
async def _start(ctx: Any, args: Mapping[str, Any]) -> dict[str, Any]:
    return await fermentation.start_fermentation(ctx, dict(args))


@op("fermentation.advance_stage", mutating=True)
async def _advance_stage(ctx: Any, args: Mapping[str, Any]) -> dict[str, Any]:
    return await fermentation.advance_stage_manual(ctx, dict(args))


@op("fermentation.terminate", mutating=True)
async def _terminate(ctx: Any, args: Mapping[str, Any]) -> dict[str, Any]:
    return await fermentation.terminate_fermentation(ctx, dict(args))


@op("fermentation.update_stages", mutating=True)
async def _update_stages(ctx: Any, args: Mapping[str, Any]) -> dict[str, Any]:
    return await fermentation.update_stages(ctx, dict(args))


@op("fermentation.set_stage_enabled", mutating=True)
async def _set_stage_enabled(ctx: Any, args: Mapping[str, Any]) -> dict[str, Any]:
    return await fermentation.set_stage_enabled(ctx, dict(args))


@op("fermentation.insert_stage", mutating=True)
async def _insert_stage(ctx: Any, args: Mapping[str, Any]) -> dict[str, Any]:
    return await fermentation.insert_stage(ctx, dict(args))


@op("fermentation.reorder_stages", mutating=True)
async def _reorder_stages(ctx: Any, args: Mapping[str, Any]) -> dict[str, Any]:
    return await fermentation.reorder_stages(ctx, dict(args))
