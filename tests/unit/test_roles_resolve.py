"""Consumes tests/fixtures/mapping_cases.json -- the same table a future
TypeScript port of this algorithm (for instant Hardware Setup UI feedback)
must also pass, so the two implementations can't silently drift apart.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from krauken.contracts.models import DeviceCandidate, Health
from krauken.contracts.roles import Role, resolve

FIXTURE_PATH = Path(__file__).parents[1] / "fixtures" / "mapping_cases.json"
FIXTURE = json.loads(FIXTURE_PATH.read_text())


def _build_devices() -> dict[str, DeviceCandidate]:
    out = {}
    for device_id, spec in FIXTURE["devices"].items():
        out[device_id] = DeviceCandidate(
            device_id=device_id,
            platform="test",
            display_name=spec["display_name"],
            kind_label="",
            capabilities=frozenset(Role(r) for r in spec["capabilities"]),
            bundled_roles=frozenset(Role(r) for r in spec["bundled_roles"]),
            health=Health.OK,
        )
    return out


DEVICES = _build_devices()


@pytest.mark.parametrize("case", FIXTURE["cases"], ids=[c["name"] for c in FIXTURE["cases"]])
def test_mapping_case(case: dict):
    draft = {Role(k): v for k, v in case["draft"].items()}
    result = resolve(draft, DEVICES)

    expected_roles = {Role(k): v for k, v in case["expected_roles"].items()}
    actual_roles = {r: result.roles.get(r) for r in Role}
    assert actual_roles == expected_roles, case["name"]

    actual_notices = sorted(
        (n.device_id, sorted(r.value for r in n.roles_cleared)) for n in result.auto_resolved
    )
    expected_notices = sorted(
        (n["device_id"], sorted(n["roles_cleared"])) for n in case["expected_auto_resolved"]
    )
    assert actual_notices == expected_notices, case["name"]

    actual_blocking_codes = sorted(b.code for b in result.blocking)
    assert actual_blocking_codes == sorted(case["expected_blocking_codes"]), case["name"]

    assert result.valid == (len(case["expected_blocking_codes"]) == 0)
