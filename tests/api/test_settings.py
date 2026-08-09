"""GET/PUT /settings/{key} -- a generic key/value store, currently used only
by the Hardware Setup wizard's chamber-location step, but not bespoke to it.
"""
from __future__ import annotations

from httpx import AsyncClient


async def test_unknown_key_reads_as_null(client: AsyncClient):
    resp = await client.get("/api/v1/settings/chamber_location")
    assert resp.status_code == 200
    assert resp.json() == {"key": "chamber_location", "value": None}


async def test_save_then_read_round_trips(client: AsyncClient):
    save = await client.put("/api/v1/settings/chamber_location", json={"value": "Garage"})
    assert save.status_code == 200
    assert save.json() == {"key": "chamber_location", "value": "Garage"}

    read = await client.get("/api/v1/settings/chamber_location")
    assert read.json() == {"key": "chamber_location", "value": "Garage"}


async def test_save_overwrites_previous_value(client: AsyncClient):
    await client.put("/api/v1/settings/chamber_location", json={"value": "Garage"})
    await client.put("/api/v1/settings/chamber_location", json={"value": "Basement"})

    read = await client.get("/api/v1/settings/chamber_location")
    assert read.json()["value"] == "Basement"


async def test_save_without_client_header_is_rejected(raw_client: AsyncClient):
    resp = await raw_client.put("/api/v1/settings/chamber_location", json={"value": "Garage"})
    assert resp.status_code == 403
