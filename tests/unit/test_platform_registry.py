from __future__ import annotations

import pytest

from krauken.contracts.roles import CHAMBER_BUNDLE, Role
from krauken.platforms.registry import build_registry


@pytest.mark.asyncio
async def test_default_registry_has_manual_and_simulator():
    registry = build_registry()
    ids = {d.platform_id for d in registry}
    assert ids == {"manual", "simulator"}


@pytest.mark.asyncio
async def test_simulator_emits_two_independent_candidates():
    # Per project decision: the simulator must emit a chamber candidate AND
    # a separate hydrometer-like candidate, not one combined device -- so a
    # pure-sim dev environment still exercises the bundle rule and the
    # independent-beer-temp-source path, not a shortcut around them.
    registry = build_registry()
    simulator = next(d for d in registry if d.platform_id == "simulator")
    candidates = await simulator.discover({})
    assert len(candidates) == 2
    by_id = {c.device_id: c for c in candidates}
    assert by_id["simulator:chamber"].bundled_roles == CHAMBER_BUNDLE
    assert by_id["simulator:tilt"].bundled_roles == frozenset()
    assert Role.BEER_GRAVITY in by_id["simulator:tilt"].capabilities


@pytest.mark.asyncio
async def test_manual_candidates_are_marked_simulated():
    registry = build_registry()
    manual = next(d for d in registry if d.platform_id == "manual")
    candidates = await manual.discover({})
    assert all(c.simulated for c in candidates)
