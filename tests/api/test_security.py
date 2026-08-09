"""The anti-CSRF header check itself. Every other test file's `client`
fixture sends this header by default (matching what a real browser's fetch
wrapper does) -- this file deliberately uses `raw_client`, which omits it,
to verify the enforcement is real, not just a client-side header nobody
checks (which is exactly the gap this middleware was written to close).
"""
from __future__ import annotations

from httpx import AsyncClient


async def test_mutating_request_without_header_is_rejected(raw_client: AsyncClient):
    resp = await raw_client.post("/api/v1/hardware/scan")
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "missing_client_header"


async def test_get_request_without_header_is_allowed(raw_client: AsyncClient):
    resp = await raw_client.get("/api/v1/health")
    assert resp.status_code == 200
