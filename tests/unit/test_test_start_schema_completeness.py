"""Guards against the exact bug class api/schemas.py's TestStartRequest
docstring already calls out once (confirm_heater was missing from its
Literal entirely, so a real API call was rejected by pydantic with a 422
before ever reaching daemon/tests_runtime.py) -- and which calibrate_gravity
(platforms/tilt/calibration.py) hit again for real, live, the same way:
dispatch-level tests (test_tilt_calibration.py) call
daemon/tests_runtime.py:start_test() directly, which has no idea
TestStartRequest's Literal even exists, so they can never catch a new
action missing from it.

This test enumerates every action string daemon/tests_runtime.py can
actually dispatch -- its own fixed elif chain, plus every
PLATFORM_BINDINGS[...].test_runners key -- and asserts TestStartRequest's
Literal is a superset. A future action added to either place without a
matching Literal entry fails HERE, at the schema/dispatch boundary,
instead of only in production the first time a real API call hits it."""
from __future__ import annotations

from typing import get_args

from krauken.api.schemas import TestStartRequest
from krauken.platforms.registry import PLATFORM_BINDINGS

# daemon/tests_runtime.py:start_test()'s own fixed if/elif branches --
# not derived from that module (nothing there exports this list), so this
# needs updating by hand if a new fixed action is ever added there. The
# per-platform PLATFORM_BINDINGS.test_runners tables below are derived,
# not hand-maintained, since those already exist as real data.
FIXED_DISPATCH_ACTIONS = frozenset({"fire_outlet", "identify_probes", "confirm_heater", "live_read"})


def test_every_dispatchable_action_is_accepted_by_the_test_start_schema():
    platform_actions = {
        action for binding in PLATFORM_BINDINGS.values() for action in binding.test_runners
    }
    dispatchable = FIXED_DISPATCH_ACTIONS | platform_actions

    schema_actions = set(get_args(TestStartRequest.model_fields["action"].annotation))

    missing = dispatchable - schema_actions
    assert not missing, (
        f"{missing} can be dispatched by daemon/tests_runtime.py but would be rejected "
        "with a 422 before ever reaching it -- add them to TestStartRequest's Literal "
        "in api/schemas.py."
    )
