from __future__ import annotations

from fastapi import APIRouter, Depends


from krauken.api import deps
from krauken.api.schemas import (
    DeviceResponse,
    MappingGetResponse,
    MappingSaveRequest,
    MappingSaveResponse,
    ScanStartResponse,
    ScanStatusResponse,
    TestResponse,
    TestStartRequest,
)
from krauken.db import queries
from krauken.ipc.client import AsyncIPCClient

router = APIRouter()


@router.post("/hardware/scan", response_model=ScanStartResponse)
async def start_scan(client: AsyncIPCClient = Depends(deps.daemon)) -> ScanStartResponse:
    result = await client.call("hardware.scan_start")
    return ScanStartResponse(**result)


@router.get("/hardware/scan/{scan_id}", response_model=ScanStatusResponse)
async def get_scan_status(
    scan_id: str,
    client: AsyncIPCClient = Depends(deps.daemon),
) -> ScanStatusResponse:
    status = await client.call("hardware.scan_status", {"scan_id": scan_id})
    # Devices themselves are read straight from SQLite (the daemon persists
    # them there as the scan runs) -- the job's `state`/`platform_status`
    # are the only things that are genuinely daemon-only truth.
    devices = await deps.run_ro(queries.devices) if status["state"] == "complete" else []
    return ScanStatusResponse(**status, devices=[DeviceResponse(**d) for d in devices])


@router.get("/hardware/devices", response_model=list[DeviceResponse])
async def get_devices() -> list[DeviceResponse]:
    devices = await deps.run_ro(queries.devices)
    return [DeviceResponse(**d) for d in devices]


@router.get("/hardware/mapping", response_model=MappingGetResponse)
async def get_mapping() -> MappingGetResponse:
    mapping = await deps.run_ro(queries.hardware_mapping)
    return MappingGetResponse(**mapping)


@router.put("/hardware/mapping", response_model=MappingSaveResponse)
async def save_mapping(
    body: MappingSaveRequest, client: AsyncIPCClient = Depends(deps.daemon)
) -> MappingSaveResponse:
    result = await client.call("hardware.mapping_save", {"roles": body.roles})
    return MappingSaveResponse(**result)


@router.post("/hardware/devices/{device_id}/test", response_model=TestResponse)
async def start_test(
    device_id: str, body: TestStartRequest, client: AsyncIPCClient = Depends(deps.daemon)
) -> TestResponse:
    result = await client.call(
        "hardware.test_start", {"device_id": device_id, "action": body.action, "params": body.params}
    )
    return TestResponse(**result)


@router.get("/hardware/devices/{device_id}/test/{test_id}", response_model=TestResponse)
async def get_test_status(
    device_id: str, test_id: str, client: AsyncIPCClient = Depends(deps.daemon)
) -> TestResponse:
    result = await client.call("hardware.test_status", {"test_id": test_id})
    return TestResponse(**result)


@router.post("/hardware/devices/{device_id}/test/{test_id}/cancel", response_model=TestResponse)
async def cancel_test(
    device_id: str, test_id: str, client: AsyncIPCClient = Depends(deps.daemon)
) -> TestResponse:
    result = await client.call("hardware.test_cancel", {"test_id": test_id})
    return TestResponse(**result)
