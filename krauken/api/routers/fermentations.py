from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

from krauken.api import deps
from krauken.api.schemas import (
    AdvanceStageResponse,
    AlertResponse,
    FermentationDetail,
    FermentationStartRequest,
    FermentationStartResponse,
    FermentationSummary,
    SeriesResponse,
    SetStageEnabledRequest,
    SetStageEnabledResponse,
    TerminateRequest,
    TerminateResponse,
    UpdateStagesRequest,
    UpdateStagesResponse,
)
from krauken.db import queries
from krauken.ipc.client import AsyncIPCClient

router = APIRouter()


@router.get("/fermentations", response_model=list[FermentationSummary])
async def list_fermentations(
    status: str | None = Query(default=None),
    limit: int = Query(default=50, le=200),
) -> list[FermentationSummary]:
    rows = await deps.run_ro(lambda conn: queries.fermentations_list(conn, status=status, limit=limit))
    return [FermentationSummary(**f) for f in rows]


@router.get("/fermentations/{fermentation_id}", response_model=FermentationDetail)
async def get_fermentation(fermentation_id: int) -> FermentationDetail:
    detail = await deps.run_ro(queries.fermentation_detail, fermentation_id)
    if detail is None:
        raise HTTPException(status_code=404, detail=f"fermentation {fermentation_id} not found")
    return FermentationDetail(**detail)


@router.get("/fermentations/{fermentation_id}/series", response_model=SeriesResponse)
async def get_series(fermentation_id: int) -> SeriesResponse:
    series = await deps.run_ro(queries.fermentation_series, fermentation_id)
    # SeriesGap's fields are populated via its "from" alias directly from
    # each gap dict -- no need to reconstruct them here (an earlier draft
    # did, and also passed `gaps=` alongside `**series`, which already
    # contains a "gaps" key -- that raises "multiple values for keyword
    # argument" outright).
    return SeriesResponse(**series)


@router.post("/fermentations", response_model=FermentationStartResponse)
async def start_fermentation(
    body: FermentationStartRequest, client: AsyncIPCClient = Depends(deps.daemon)
) -> FermentationStartResponse:
    result = await client.call(
        "fermentation.start",
        {
            "name": body.name, "yeast_id": body.yeast_id, "yeast_name": body.yeast_name, "og": body.og,
            "stages": [s.model_dump() for s in body.stages],
        },
    )
    return FermentationStartResponse(**result)


@router.post("/fermentations/{fermentation_id}/advance", response_model=AdvanceStageResponse)
async def advance_stage(fermentation_id: int, client: AsyncIPCClient = Depends(deps.daemon)) -> AdvanceStageResponse:
    result = await client.call("fermentation.advance_stage", {"fermentation_id": fermentation_id})
    return AdvanceStageResponse(**result)


@router.post("/fermentations/{fermentation_id}/terminate", response_model=TerminateResponse)
async def terminate_fermentation(
    fermentation_id: int, body: TerminateRequest, client: AsyncIPCClient = Depends(deps.daemon)
) -> TerminateResponse:
    result = await client.call("fermentation.terminate", {"fermentation_id": fermentation_id, "reason": body.reason})
    return TerminateResponse(**result)


@router.put("/fermentations/{fermentation_id}/stages", response_model=UpdateStagesResponse)
async def update_stages(
    fermentation_id: int, body: UpdateStagesRequest, client: AsyncIPCClient = Depends(deps.daemon)
) -> UpdateStagesResponse:
    result = await client.call(
        "fermentation.update_stages", {"fermentation_id": fermentation_id, "stages": body.stages}
    )
    return UpdateStagesResponse(**result)


@router.put("/fermentations/{fermentation_id}/stages/{stage_id}/enabled", response_model=SetStageEnabledResponse)
async def set_stage_enabled(
    fermentation_id: int, stage_id: int, body: SetStageEnabledRequest, client: AsyncIPCClient = Depends(deps.daemon)
) -> SetStageEnabledResponse:
    result = await client.call(
        "fermentation.set_stage_enabled",
        {"fermentation_id": fermentation_id, "stage_id": stage_id, "enabled": body.enabled},
    )
    return SetStageEnabledResponse(**result)


@router.get("/fermentations/{fermentation_id}/alerts", response_model=list[AlertResponse])
async def get_alerts(fermentation_id: int) -> list[AlertResponse]:
    alerts = await deps.run_ro(queries.active_alerts, fermentation_id)
    return [AlertResponse(**a) for a in alerts]
