from __future__ import annotations

import pytest

from krauken.contracts.errors import PlatformUnavailable
from krauken.platforms.base import requires_optional


async def test_raises_platform_unavailable_for_a_module_that_does_not_exist():
    @requires_optional("this_module_definitely_does_not_exist_anywhere")
    async def fn():
        return "should never get here"

    with pytest.raises(PlatformUnavailable, match="pip install"):
        await fn()


async def test_passes_through_when_the_module_is_importable():
    @requires_optional("json")  # any real stdlib module -- always importable
    async def fn(x):
        return x * 2

    assert await fn(3) == 6
