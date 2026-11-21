"""Schema演进接口：维护结构变更，覆盖执行变更、标记生效、回滚变更等动作。"""
from __future__ import annotations

import csv
import io
import json

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from app.schemas import ActionPayload, ActionResult, EntryPayload, PageResult
from app.services.schema_evolve import CHECK_FIELDS, SchemaEvolveService
from app.services.compatibility import CompatibilityService

router = APIRouter(prefix="/api/schema_evolve", tags=["Schema演进"])

service = SchemaEvolveService()
compat_service = CompatibilityService()

LIST_FIELDS = ["变更编号", "关联流", "变更类型", "新增字段", "删除字段", "兼容级别", "生效版本", "回滚标记"]
STATUSES = ["草稿", "待执行", "已生效", "已回滚"]


def query_filters(request: Request) -> dict[str, str]:
    """只接受当前模块声明过的字段，忽略分页等保留参数。"""
    return {
        key: value.strip()
        for key, value in request.query_params.items()
        if key in LIST_FIELDS and value.strip()
    }


@router.get("", response_model=PageResult[dict])
def list_entries(
    request: Request,
    keyword: str | None = Query(default=None, description="按变更编号检索"),
    status: str | None = Query(default=None, description="草稿、待执行、已生效、已回滚"),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=200),
) -> PageResult[dict]:
    """按页面字段、变更编号关键字与状态过滤Schema演进列表。"""
    items, total = service.list_entries(
        filters=query_filters(request), keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/export")
def export_entries(
    request: Request,
    keyword: str | None = Query(default=None),
    status: str | None = Query(default=None),
) -> StreamingResponse:
    """下载当前筛选条件下的 UTF-8 CSV；必须声明在 /{entry_id} 之前。"""
    items, _ = service.list_entries(
        filters=query_filters(request), keyword=keyword, status=status, page=1, size=1000000)
    output = io.StringIO()
    output.write("\ufeff")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(["id", *LIST_FIELDS, *CHECK_FIELDS, "status"])
    for item in items:
        row = []
        for value in [
            item.get("id"),
            *(item.get(field, "") for field in LIST_FIELDS),
            *(item.get(field, "") for field in CHECK_FIELDS),
            item.get("status", ""),
        ]:
            if isinstance(value, (list, dict)):
                text = json.dumps(value, ensure_ascii=False)
            else:
                text = "" if value is None else str(value)
            row.append("'" + text if text.startswith(("=", "+", "-", "@")) else text)
        writer.writerow(row)
    return StreamingResponse(
        iter([output.getvalue()]), media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="schema_evolve.csv"'})


@router.get("/compatibility/candidates")
def list_compat_candidates() -> dict:
    """可做兼容转换的字段改名变更；必须声明在 /{entry_id} 之前。"""
    items = compat_service.change_candidates()
    return {"ok": True, "items": items, "total": len(items)}


@router.get("/compatibility/batches")
def list_compat_batches() -> dict:
    """兼容转换批次摘要；必须声明在 /{entry_id} 之前。"""
    items = compat_service.list_batches()
    return {"ok": True, "items": items, "total": len(items)}


@router.post("/compatibility/run", response_model=ActionResult)
def run_compat(payload: EntryPayload) -> ActionResult:
    """由结构变更生成转换兼容数据批次，并把校验结果回写结构核对清单。

    ``values`` 支持 change_id / batch_no / effective_boundary /
    old_pipeline_backfill / records / row_key / offset_field。
    """
    values = payload.values
    change_id = values.get("change_id") or values.get("变更ID") or values.get("变更编号")
    if change_id in (None, ""):
        return ActionResult(ok=False, message="缺少字段：change_id（结构变更 id 或变更编号）")
    batch, message = compat_service.run(
        change_id=change_id,
        records=values.get("records"),
        batch_no=values.get("batch_no"),
        row_key=str(values.get("row_key") or "订单编号"),
        offset_field=str(values.get("offset_field") or "位点"),
        effective_boundary=values.get("effective_boundary"),
        old_pipeline_backfill=bool(values.get("old_pipeline_backfill")),
        legacy_version=values.get("legacy_version"),
        new_version=values.get("new_version"),
    )
    if batch is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=batch)


@router.get("/checks")
def list_checks(
    change_id: int | str | None = Query(default=None),
    batch_no: str | None = Query(default=None),
) -> dict:
    """结构核对清单；必须声明在 /{entry_id} 之前。"""
    items = compat_service.list_checks(change_id=change_id, batch_no=batch_no)
    return {"ok": True, "items": items, "total": len(items)}


@router.get("/reviews")
def list_reviews(
    batch_no: str | None = Query(default=None),
    change_id: int | str | None = Query(default=None),
    status: str | None = Query(default=None),
) -> dict:
    """待核对区：两列金额冲突的原行；必须声明在 /{entry_id} 之前。"""
    items = compat_service.list_reviews(batch_no=batch_no, change_id=change_id, status=status)
    return {"ok": True, "items": items, "total": len(items)}


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条结构变更明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"结构变更 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: dict[str, object]) -> ActionResult:
    """登记一条结构变更，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="结构变更已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: ActionPayload) -> ActionResult:
    """对单条结构变更执行执行变更、标记生效、回滚变更；不允许的动作会被拦下并说明原因。"""
    entry, message = service.run_action(entry_id, payload.action.strip())
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
