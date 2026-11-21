"""兼容转换接口：由结构变更生成转换兼容数据批次，并回写结构核对清单。

接口只做参数整形与错误转成可读消息，规则全部落在
``app.services.compatibility.CompatibilityService``。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.schemas import ActionResult
from app.services.compatibility import CompatibilityService

router = APIRouter(prefix="/api/compatibility", tags=["兼容转换"])
service = CompatibilityService()


class CompatRunPayload(BaseModel):
    """生成或续做兼容批次的入参。"""

    change_id: int | str = Field(description="结构变更的 id 或变更编号")
    records: list[dict[str, Any]] | None = Field(
        default=None, description="订单记录；省略时使用内置的改名订单流样本")
    batch_no: str | None = Field(default=None, description="续做已有批次时传入批次号")
    row_key: str = Field(default="订单编号", description="原行标识字段")
    offset_field: str = Field(default="位点", description="生效边界使用的位点字段")
    effective_boundary: int | float | None = Field(default=None, description="新旧字段的生效边界位点")
    old_pipeline_backfill: bool = Field(default=False, description="旧流水线是否仍在补算")
    legacy_version: str | None = None
    new_version: str | None = None


@router.get("/candidates")
def list_candidates() -> dict[str, Any]:
    """列出可做兼容转换的字段改名记录。"""
    return {"ok": True, "items": service.change_candidates()}


@router.get("/batches")
def list_batches() -> dict[str, Any]:
    """兼容批次摘要列表。"""
    items = service.list_batches()
    return {"ok": True, "items": items, "total": len(items)}


@router.get("/batches/{batch_id}")
def get_batch(batch_id: int) -> dict[str, Any]:
    """单批次明细：逐行兼容视图、统计、字段版本依赖与数据库写入范围。"""
    batch = service.get_batch(batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail=f"兼容批次 {batch_id} 不存在")
    return batch


@router.get("/batches/{batch_id}/rollback-trace")
def rollback_trace(batch_id: int) -> dict[str, Any]:
    """按原行标识给出回滚到原列的追溯结果。"""
    trace, message = service.rollback_trace(batch_id)
    if trace is None:
        raise HTTPException(status_code=404, detail=message)
    return {"ok": True, "trace": trace}


@router.get("/reviews")
def list_reviews(
    batch_no: str | None = Query(default=None),
    change_id: int | str | None = Query(default=None),
    status: str | None = Query(default=None),
) -> dict[str, Any]:
    """待核对区：两列金额冲突的行，按原行标识可定位。"""
    items = service.list_reviews(batch_no=batch_no, change_id=change_id, status=status)
    return {"ok": True, "items": items, "total": len(items)}


@router.get("/checks")
def list_checks(
    change_id: int | str | None = Query(default=None),
    batch_no: str | None = Query(default=None),
) -> dict[str, Any]:
    """结构核对清单：兼容转换把校验结果回写到这里。"""
    items = service.list_checks(change_id=change_id, batch_no=batch_no)
    return {"ok": True, "items": items, "total": len(items)}


@router.post("/run", response_model=ActionResult)
def run_compat(payload: CompatRunPayload) -> ActionResult:
    """读取结构变更记录生成兼容批次；同一批次号重复执行可续做。"""
    batch, message = service.run(
        change_id=payload.change_id,
        records=payload.records,
        batch_no=payload.batch_no,
        row_key=payload.row_key,
        offset_field=payload.offset_field,
        effective_boundary=payload.effective_boundary,
        old_pipeline_backfill=payload.old_pipeline_backfill,
        legacy_version=payload.legacy_version,
        new_version=payload.new_version,
    )
    if batch is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=batch)
