"""Direct, dependency-free unit tests against SimulatorPlatform.discover()
-- no IPC/sockets needed, unlike test_platform_registry.py's real-service
coverage of the same platform. Specifically covers the
KRAUKEN_SIMULATOR_TILT_ENABLED gate (default on, since Krauken isn't in
production yet -- see platform.py's own module-level comment)."""
from __future__ import annotations

import pytest

from krauken.platforms.simulator.platform import SimulatorPlatform


async def test_default_emits_both_chamber_and_tilt_candidates():
    platform = SimulatorPlatform()
    candidates = await platform.discover({})
    ids = {c.device_id for c in candidates}
    assert ids == {"simulator:chamber", "simulator:tilt"}


async def test_tilt_enabled_explicitly_set_to_1_still_emits_both(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("KRAUKEN_SIMULATOR_TILT_ENABLED", "1")
    platform = SimulatorPlatform()
    candidates = await platform.discover({})
    ids = {c.device_id for c in candidates}
    assert ids == {"simulator:chamber", "simulator:tilt"}


async def test_tilt_disabled_via_env_var_yields_only_chamber(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("KRAUKEN_SIMULATOR_TILT_ENABLED", "0")
    platform = SimulatorPlatform()
    candidates = await platform.discover({})
    ids = {c.device_id for c in candidates}
    assert ids == {"simulator:chamber"}
