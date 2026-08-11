from __future__ import annotations

from httpx import AsyncClient


async def test_yeasts_returns_the_shipped_presets(client: AsyncClient):
    resp = await client.get("/api/v1/yeasts")
    assert resp.status_code == 200
    yeasts = resp.json()["yeasts"]
    assert "us05" in yeasts
    assert yeasts["us05"]["name"] == "SafAle US-05 - American ale"
    assert yeasts["us05"]["default_stages"][0]["temp_f"] == 66.0
    # A strain that genuinely doesn't need one has no diacetyl-rest-shaped
    # entry at all -- driven by what's in the data file, not a flag the API
    # or UI has to interpret.
    assert len(yeasts["lutra_kveik"]["default_stages"]) == 2
