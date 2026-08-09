from __future__ import annotations

from fastapi import APIRouter, Depends

from krauken.api import deps
from krauken.api.schemas import SettingResponse, SettingSaveRequest
from krauken.db import queries
from krauken.ipc.client import AsyncIPCClient

router = APIRouter()


@router.get("/settings/{key}", response_model=SettingResponse)
async def get_setting(key: str) -> SettingResponse:
    value = await deps.run_ro(queries.setting, key)
    return SettingResponse(key=key, value=value)


@router.put("/settings/{key}", response_model=SettingResponse)
async def save_setting(
    key: str, body: SettingSaveRequest, client: AsyncIPCClient = Depends(deps.daemon)
) -> SettingResponse:
    result = await client.call("settings.save", {"key": key, "value": body.value})
    return SettingResponse(**result)
