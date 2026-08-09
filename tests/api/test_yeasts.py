from __future__ import annotations

from httpx import AsyncClient


async def test_yeasts_returns_the_shipped_presets(client: AsyncClient):
    resp = await client.get("/api/v1/yeasts")
    assert resp.status_code == 200
    yeasts = resp.json()["yeasts"]
    assert "us05" in yeasts
    assert yeasts["us05"]["name"] == "SafAle US-05 - American ale"
    assert yeasts["us05"]["stage_defaults"]["primary"]["temp_f"] == 66.0
