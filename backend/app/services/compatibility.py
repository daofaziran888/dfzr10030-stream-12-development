"""结构变更期间的兼容转换：把改名前后的金额列收敛成同一批可读数据。

背景
----
``stream_transform`` 原本固定读取 ``金额`` 列。表结构改名后，上游订单流里会同时
出现旧列、新列、两列都有四种形态；旧报表只认旧列，新报表只认新列，而旧流水线
可能还在补算。这个服务读取 ``schema_evolve`` 的结构变更记录，按变更里的
“删除字段 -> 新增字段”映射生成一份**转换兼容数据批次**：

* 每个非争议行同时给出旧版视图与新版视图，两个下游版本都能找到可读字段；
* 旧版视图保留历史订单的原列取值与字段版本，不重新解释历史数据；
* 两列金额不一致时，按原行标识（订单编号）转入待核对区，兼容视图与聚合都跳过；
* 同一批次重复执行按行标识幂等，迁移中断后可以续做，已确认的金额不会被写两次；
* 批次记录生效边界、字段版本依赖和数据库实际写入范围，回滚时可按原列追溯。

结果同时回写到 ``schema_evolve`` 的“结构核对清单”，供页面查看。
"""
from __future__ import annotations

import copy
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from app.store import store

SCHEMA_MODULE = "schema_evolve"
BATCH_MODULE = "_compat_batches"
REVIEW_MODULE = "_compat_reviews"
CHECK_MODULE = "_schema_checks"

RENAME_HINTS = ("改名", "重命名", "rename")
REVIEW_STATUS_PENDING = "待核对"
LEGACY_VERSION_FALLBACK = "v1"
NEW_VERSION_FALLBACK = "v2"
DEFAULT_ROW_KEY = "订单编号"
DEFAULT_OFFSET_FIELD = "位点"


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _to_decimal(value: Any) -> Decimal | None:
    """把金额转成 Decimal；空值、布尔值和不可解析文本都视为没有金额。"""
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip().replace(",", "")
    if not text:
        return None
    try:
        return Decimal(text)
    except (InvalidOperation, ValueError):
        return None


def _to_float(value: Decimal | None) -> float | None:
    return None if value is None else float(round(value, 2))


def _next_id(rows: list[dict[str, Any]]) -> int:
    return max((int(row.get("id", 0)) for row in rows), default=0) + 1


def _strip_column(name: Any) -> str:
    text = _text(name)
    return text.strip("`\"' ")


class CompatibilityService:
    # ------------------------------------------------------------------
    # 结构变更记录解析
    # ------------------------------------------------------------------
    def resolve_change(self, change_id: int | str) -> tuple[dict[str, Any] | None, str]:
        """按数据库 id 或变更编号找到一条结构变更。"""
        if isinstance(change_id, int) or (isinstance(change_id, str) and change_id.isdigit()):
            entry = store.find(SCHEMA_MODULE, int(change_id))
            if entry is not None:
                return entry, ""
        target = _text(change_id)
        if target:
            for row in store.rows(SCHEMA_MODULE):
                if _text(row.get("变更编号")) == target:
                    return row, ""
        return None, f"结构变更 {change_id} 不存在或已归档"

    def resolve_rename(self, change: dict[str, Any]) -> tuple[str, str, str]:
        """从变更记录里解析“旧列 -> 新列”；不是字段改名的记录不能做兼容转换。"""
        change_type = _text(change.get("变更类型"))
        if not any(hint in change_type.casefold() for hint in RENAME_HINTS):
            return "", "", (
                f"结构变更「{_text(change.get('变更编号')) or change.get('id')}」的类型是"
                f"「{change_type or '未填写'}」，只有字段改名才能生成兼容批次"
            )

        removed = _strip_column(change.get("删除字段"))
        added = _strip_column(change.get("新增字段"))
        if "->" in removed:
            old_field, _, new_field = removed.partition("->")
            old_field, new_field = _strip_column(old_field), _strip_column(new_field)
        elif "->" in added:
            old_field, _, new_field = added.partition("->")
            old_field, new_field = _strip_column(old_field), _strip_column(new_field)
        else:
            old_field, new_field = removed, added

        if not old_field or not new_field:
            return "", "", "结构变更缺少改名映射：请在“删除字段”和“新增字段”中分别填写旧列与新列"
        if old_field == new_field:
            return "", "", f"结构变更的旧列与新列都是「{old_field}」，无法生成兼容转换"
        return old_field, new_field, ""

    def field_versions(self, change: dict[str, Any], legacy_version: str | None, new_version: str | None) -> tuple[str, str]:
        new = _text(new_version) or _text(change.get("生效版本")) or NEW_VERSION_FALLBACK
        legacy = _text(legacy_version) or _text(change.get("旧字段版本")) or LEGACY_VERSION_FALLBACK
        if legacy == new:
            legacy = f"{new}-prev"
        return legacy, new

    # ------------------------------------------------------------------
    # 查询入口
    # ------------------------------------------------------------------
    def list_batches(self) -> list[dict[str, Any]]:
        return [self._batch_summary(row) for row in store.rows(BATCH_MODULE)]

    def get_batch(self, batch_id: int) -> dict[str, Any] | None:
        return store.find(BATCH_MODULE, batch_id)

    def find_batch_by_no(self, batch_no: str) -> dict[str, Any] | None:
        target = _text(batch_no)
        for row in store.rows(BATCH_MODULE):
            if _text(row.get("批次号")) == target:
                return row
        return None

    def list_reviews(
        self,
        *,
        batch_no: str | None = None,
        change_id: int | str | None = None,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        rows = store.rows(REVIEW_MODULE)
        if batch_no:
            rows = [row for row in rows if _text(row.get("批次号")) == _text(batch_no)]
        if change_id not in (None, ""):
            rows = [row for row in rows if _text(row.get("变更ID")) == _text(change_id)]
        if status:
            rows = [row for row in rows if _text(row.get("状态")) == status]
        return rows

    def list_checks(self, *, change_id: int | str | None = None, batch_no: str | None = None) -> list[dict[str, Any]]:
        rows = store.rows(CHECK_MODULE)
        if change_id not in (None, ""):
            rows = [row for row in rows if _text(row.get("变更ID")) == _text(change_id)]
        if batch_no:
            rows = [row for row in rows if _text(row.get("批次号")) == _text(batch_no)]
        return rows

    def change_candidates(self) -> list[dict[str, Any]]:
        """给页面列出可以做兼容转换的字段改名记录。"""
        candidates: list[dict[str, Any]] = []
        for change in store.rows(SCHEMA_MODULE):
            old_field, new_field, error = self.resolve_rename(change)
            if error:
                continue
            candidates.append({
                "id": change.get("id"),
                "变更编号": change.get("变更编号"),
                "关联流": change.get("关联流"),
                "变更类型": change.get("变更类型"),
                "旧字段": old_field,
                "新字段": new_field,
                "生效版本": change.get("生效版本"),
                "status": change.get("status"),
                "已回滚": change.get("status") == "已回滚",
            })
        return candidates

    # ------------------------------------------------------------------
    # 生成兼容批次（幂等、可续做）
    # ------------------------------------------------------------------
    def run(
        self,
        *,
        change_id: int | str,
        records: list[dict[str, Any]] | None = None,
        batch_no: str | None = None,
        row_key: str = DEFAULT_ROW_KEY,
        offset_field: str = DEFAULT_OFFSET_FIELD,
        effective_boundary: int | float | None = None,
        old_pipeline_backfill: bool = False,
        legacy_version: str | None = None,
        new_version: str | None = None,
    ) -> tuple[dict[str, Any] | None, str]:
        change, error = self.resolve_change(change_id)
        if change is None:
            return None, error
        old_field, new_field, error = self.resolve_rename(change)
        if error:
            return None, error

        row_key = _text(row_key) or DEFAULT_ROW_KEY
        offset_field = _text(offset_field) or DEFAULT_OFFSET_FIELD
        if row_key in (old_field, new_field):
            return None, f"行标识字段「{row_key}」不能与金额列同名"

        if records is None:
            from app.fixtures import MIGRATION_ORDER_RECORDS
            records = list(MIGRATION_ORDER_RECORDS)
        if not isinstance(records, list) or not records:
            return None, "没有可转换的订单记录"
        if change.get("status") == "已回滚":
            return None, f"结构变更「{_text(change.get('变更编号')) or change.get('id')}」已回滚，不能再生成兼容批次"

        legacy_version, new_version = self.field_versions(change, legacy_version, new_version)
        batch, error = self._open_batch(
            change=change, batch_no=batch_no, old_field=old_field, new_field=new_field,
            legacy_version=legacy_version, new_version=new_version,
            row_key=row_key, offset_field=offset_field,
            effective_boundary=effective_boundary, old_pipeline_backfill=old_pipeline_backfill,
        )
        if batch is None:
            return None, error

        processed: set[str] = set(str(key) for key in batch["已处理行标识"])
        seen_in_payload: set[str] = set()
        added = 0

        for index, raw in enumerate(records):
            if not isinstance(raw, dict):
                self._reject(batch, f"#{index}", "记录不是键值结构", raw)
                continue

            key = _text(raw.get(row_key))
            if not key:
                self._reject(batch, f"#{index}", f"缺少原行标识字段「{row_key}」", raw)
                continue
            if key in processed or key in seen_in_payload:
                batch["重复跳过"] = int(batch.get("重复跳过", 0)) + 1
                continue
            seen_in_payload.add(key)

            offset = self._offset(raw.get(offset_field))
            has_old = old_field in raw and _to_decimal(raw.get(old_field)) is not None
            has_new = new_field in raw and _to_decimal(raw.get(new_field)) is not None
            old_value = _to_decimal(raw.get(old_field))
            new_value = _to_decimal(raw.get(new_field))

            if not has_old and not has_new:
                if old_field in raw and _text(raw.get(old_field)):
                    self._reject(batch, key, f"旧列「{old_field}」的金额无法解析", raw)
                elif new_field in raw and _text(raw.get(new_field)):
                    self._reject(batch, key, f"新列「{new_field}」的金额无法解析", raw)
                else:
                    self._reject(batch, key, f"旧列「{old_field}」和新列「{new_field}」都没有金额", raw)
                continue

            if has_old and has_new and old_value != new_value:
                # 两列冲突：按原行标识进入待核对区，兼容视图和聚合都跳过。
                self._quarantine(
                    batch=batch, key=key, change=change, old_field=old_field, new_field=new_field,
                    old_value=old_value, new_value=new_value, offset=offset, raw=raw,
                )
                self._clear_reject(batch, key)
                processed.add(key)
                batch["已处理行标识"].append(key)
                continue

            if has_old and has_new:
                kind = "两列一致"
                canonical = old_value
                source = f"{old_field}+{new_field}"
                leg_source = old_field
                new_source = new_field
                leg_derived = False
                new_derived = False
            elif has_old:
                kind = "纯旧列"
                canonical = old_value
                source = old_field
                leg_source = old_field
                new_source = old_field
                leg_derived = False
                new_derived = True
            else:
                kind = "纯新列"
                canonical = new_value
                source = new_field
                leg_source = new_field
                new_source = new_field
                leg_derived = True
                new_derived = False

            record = {
                "原行标识": key,
                "输入形态": kind,
                "旧列": old_field,
                "旧值": _to_float(old_value),
                "新列": new_field,
                "新值": _to_float(new_value),
                "标准金额": _to_float(canonical),
                "来源列": source,
                "旧版视图": {
                    "字段": old_field,
                    "金额": _to_float(canonical),
                    "字段版本": legacy_version,
                    "来源列": leg_source,
                    "推导": leg_derived,
                },
                "新版视图": {
                    "字段": new_field,
                    "金额": _to_float(canonical),
                    "字段版本": new_version,
                    "来源列": new_source,
                    "推导": new_derived,
                },
                "位点": offset,
                "生效边界归属": self._boundary_side(offset, batch["生效边界"].get("边界值")),
                "写入次数": 1,
                "原始行": copy.deepcopy(raw),
            }
            self._collect_boundary_conflict(batch, record, kind)
            self._clear_reject(batch, key)
            batch["records"].append(record)
            processed.add(key)
            batch["已处理行标识"].append(key)
            added += 1

        batch["更新次数"] = int(batch.get("更新次数", 0)) + 1
        if added == 0 and batch["records"]:
            batch["续做说明"] = "本次输入没有新的可写入行，已按原行标识跳过重复记录"
        elif batch["records"]:
            batch["续做说明"] = f"本次新增写入 {added} 行；此前已处理的行按原行标识跳过"
        self._refresh_batch(batch, change)
        self._write_check(batch, change)
        verb = "续做完成" if _text(batch_no) else "已生成"
        return batch, (
            f"兼容批次 {batch['批次号']} {verb}：写入 {batch['统计']['转换行数']} 行，"
            f"待核对 {batch['统计']['两列冲突']} 行"
        )

    # ------------------------------------------------------------------
    # 回滚追溯
    # ------------------------------------------------------------------
    def rollback_trace(self, batch_id: int) -> tuple[dict[str, Any] | None, str]:
        batch = self.get_batch(batch_id)
        if batch is None:
            return None, f"兼容批次 {batch_id} 不存在"
        old_field = batch["旧字段"]
        new_field = batch["新字段"]
        trace_rows: list[dict[str, Any]] = []
        for record in batch.get("records", []):
            if record["输入形态"] == "纯新列":
                original_column, original_value = new_field, record["新值"]
            else:
                original_column, original_value = old_field, record["旧值"]
            trace_rows.append({
                "原行标识": record["原行标识"],
                "原列": original_column,
                "原值": original_value,
                "旧列值": record["旧值"],
                "新列值": record["新值"],
                "标准金额": record["标准金额"],
                "回滚写入": {old_field: record["标准金额"]},
            })
        trace = {
            "批次号": batch["批次号"],
            "变更编号": batch["变更编号"],
            "回滚映射": {
                "新版字段": new_field,
                "旧版字段": old_field,
                "新版版本": batch["新版本"],
                "旧版版本": batch["旧版本"],
                "回滚后读取字段": old_field,
            },
            "行追溯": trace_rows,
            "争议行": self.list_reviews(batch_no=batch["批次号"]),
            "数据库写入范围": copy.deepcopy(batch["数据库写入范围"]),
            "说明": "回滚按原行标识把兼容批次写入的金额恢复到旧列；争议行未写入，保留原始两列值待核对",
        }
        return trace, "回滚追溯已生成"

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------
    def _open_batch(
        self,
        *,
        change: dict[str, Any],
        batch_no: str | None,
        old_field: str,
        new_field: str,
        legacy_version: str,
        new_version: str,
        row_key: str,
        offset_field: str,
        effective_boundary: int | float | None,
        old_pipeline_backfill: bool,
    ) -> tuple[dict[str, Any] | None, str]:
        existing = self.find_batch_by_no(batch_no) if _text(batch_no) else None
        if existing is not None:
            if _text(existing.get("变更ID")) != _text(change.get("id")):
                return None, f"批次号「{existing['批次号']}」已用于另一条结构变更，不能续做"
            if existing["旧字段"] != old_field or existing["新字段"] != new_field:
                return None, "续做批次的字段映射与当前结构变更不一致，请核对后再执行"
            return existing, ""

        if _text(batch_no):
            number = _text(batch_no)
        else:
            number = f"COMPAT-{_text(change.get('变更编号')) or change.get('id')}"
            rows = store.rows(BATCH_MODULE)
            if any(_text(row.get("批次号")) == number for row in rows):
                suffix = 2
                while any(_text(row.get("批次号")) == f"{number}-{suffix:02d}" for row in rows):
                    suffix += 1
                number = f"{number}-{suffix:02d}"

        rows = store.rows(BATCH_MODULE)
        batch = {
            "id": _next_id(rows),
            "批次号": number,
            "变更ID": change.get("id"),
            "变更编号": change.get("变更编号"),
            "关联流": change.get("关联流"),
            "变更类型": change.get("变更类型"),
            "旧字段": old_field,
            "新字段": new_field,
            "旧版本": legacy_version,
            "新版本": new_version,
            "行键": row_key,
            "状态": "生成中",
            "生效边界": {
                "位点字段": offset_field,
                "边界值": effective_boundary,
                "旧版本": legacy_version,
                "新版本": new_version,
                "旧流水线补算中": bool(old_pipeline_backfill),
                "边界状态": (
                    "旧流水线补算中，新报表按新列读数时存在边界冲突风险"
                    if old_pipeline_backfill and effective_boundary is not None
                    else ("边界已声明" if effective_boundary is not None else "未声明生效边界")
                ),
                "冲突行数": 0,
                "冲突明细": [],
            },
            "字段版本依赖": [],
            "数据库写入范围": {
                "关联流": change.get("关联流"),
                "行键": row_key,
                "写入字段": [new_field, old_field],
                "写入行数": 0,
                "位点范围": [None, None],
                "每行写入次数": 1,
            },
            "统计": {},
            "records": [],
            "拒绝行": [],
            "已处理行标识": [],
            "重复跳过": 0,
            "更新次数": 0,
            "created_at": _now(),
            "updated_at": _now(),
        }
        rows.append(batch)
        return batch, ""

    def _offset(self, value: Any) -> int | float | None:
        if value is None or isinstance(value, bool):
            return None
        text = str(value).strip()
        if not text:
            return None
        try:
            number = Decimal(text)
        except (InvalidOperation, ValueError):
            return None
        return int(number) if number == number.to_integral_value() else float(number)

    def _boundary_side(self, offset: int | float | None, boundary: int | float | None) -> str | None:
        if offset is None or boundary is None:
            return None
        return "旧版" if offset < boundary else "新版"

    def _collect_boundary_conflict(self, batch: dict[str, Any], record: dict[str, Any], kind: str) -> None:
        boundary = batch["生效边界"].get("边界值")
        offset = record["位点"]
        if boundary is None or offset is None:
            return
        conflict = None
        if offset < boundary and kind == "纯新列":
            conflict = "新列在生效边界前已写入，旧流水线补算时可能重复覆盖"
        elif offset >= boundary and kind == "纯旧列":
            conflict = "旧列在生效边界后仍在写入，新报表读数时可能漏数"
        if conflict:
            batch["生效边界"]["冲突明细"].append({
                "原行标识": record["原行标识"],
                "位点": offset,
                "边界值": boundary,
                "输入形态": kind,
                "说明": conflict,
            })

    def _quarantine(
        self,
        *,
        batch: dict[str, Any],
        key: str,
        change: dict[str, Any],
        old_field: str,
        new_field: str,
        old_value: Decimal,
        new_value: Decimal,
        offset: int | float | None,
        raw: dict[str, Any],
    ) -> None:
        rows = store.rows(REVIEW_MODULE)
        for row in rows:
            if _text(row.get("批次号")) == batch["批次号"] and _text(row.get("原行标识")) == key:
                return
        rows.append({
            "id": _next_id(rows),
            "批次号": batch["批次号"],
            "变更ID": change.get("id"),
            "变更编号": change.get("变更编号"),
            "关联流": change.get("关联流"),
            "原行标识": key,
            "旧列": old_field,
            "旧值": _to_float(old_value),
            "新列": new_field,
            "新值": _to_float(new_value),
            "差额": _to_float(new_value - old_value),
            "位点": offset,
            "状态": REVIEW_STATUS_PENDING,
            "原因": "两列金额冲突，已排除出兼容视图与聚合，等待人工核对",
            "原始行": copy.deepcopy(raw),
            "created_at": _now(),
        })

    def _clear_reject(self, batch: dict[str, Any], key: str) -> None:
        """续做时同一行已能正常转换，撤掉上一次的拒绝记录。"""
        batch["拒绝行"] = [row for row in batch["拒绝行"] if row["原行标识"] != key]

    def _reject(self, batch: dict[str, Any], key: str, reason: str, raw: Any) -> None:
        rejected = batch["拒绝行"]
        for row in rejected:
            if row["原行标识"] == key and row["原因"] == reason:
                return
        rejected.append({"原行标识": key, "原因": reason, "原始行": copy.deepcopy(raw)})

    def _refresh_batch(self, batch: dict[str, Any], change: dict[str, Any]) -> None:
        records = batch["records"]
        reviews = self.list_reviews(batch_no=batch["批次号"])
        legacy_total = sum((_to_decimal(r["旧版视图"]["金额"]) or Decimal(0) for r in records), Decimal(0))
        new_total = sum((_to_decimal(r["新版视图"]["金额"]) or Decimal(0) for r in records), Decimal(0))
        kinds: dict[str, int] = {}
        for record in records:
            kinds[record["输入形态"]] = kinds.get(record["输入形态"], 0) + 1
        offsets = [r["位点"] for r in records if r["位点"] is not None]
        boundary = batch["生效边界"]
        boundary["冲突行数"] = len(boundary["冲突明细"])

        batch["统计"] = {
            # 输入行数只统计本批次首次见到的输入；续做时重复传入的行单独计在“重复跳过”。
            "输入行数": len(records) + len(reviews) + len(batch["拒绝行"]),
            "转换行数": len(records),
            "纯旧列": kinds.get("纯旧列", 0),
            "纯新列": kinds.get("纯新列", 0),
            "两列一致": kinds.get("两列一致", 0),
            "两列冲突": len(reviews),
            "拒绝行数": len(batch["拒绝行"]),
            "重复跳过": int(batch.get("重复跳过", 0)),
            "旧版可读行数": len(records),
            "新版可读行数": len(records),
            "已确认金额写入次数": len(records),
            "每行写入次数": 1,
            "旧版总金额": _to_float(legacy_total),
            "新版总金额": _to_float(new_total),
            "争议金额差额合计": _to_float(sum((_to_decimal(r["差额"]) or Decimal(0) for r in reviews), Decimal(0))),
        }

        by_channel: dict[str, dict[str, Any]] = {}
        for record in batch.get("records", []):
            raw = record.get("原始行") or {}
            channel = _text(raw.get("渠道")) or "未标注"
            bucket = by_channel.setdefault(channel, {"行数": 0, "金额": Decimal(0)})
            bucket["行数"] += 1
            bucket["金额"] += _to_decimal(record["标准金额"]) or Decimal(0)
        batch["聚合"] = {
            "口径": "仅聚合兼容批次已写入行；待核对争议行不参与",
            "行数": len(records),
            "金额合计": _to_float(new_total),
            "按渠道": {
                channel: {"行数": value["行数"], "金额": _to_float(value["金额"])}
                for channel, value in by_channel.items()
            },
            "跳过争议行数": len(reviews),
        }

        batch["字段版本依赖"] = [
            {
                "下游版本": batch["旧版本"],
                "读取字段": batch["旧字段"],
                "字段来源": batch["旧字段"],
                "可读行数": len(records),
                "跳过争议行数": len(reviews),
            },
            {
                "下游版本": batch["新版本"],
                "读取字段": batch["新字段"],
                "字段来源": batch["新字段"],
                "可读行数": len(records),
                "跳过争议行数": len(reviews),
            },
        ]
        batch["数据库写入范围"].update({
            "关联流": change.get("关联流"),
            "写入字段": [batch["新字段"], batch["旧字段"]],
            "写入行数": len(records),
            "位点范围": [min(offsets), max(offsets)] if offsets else [None, None],
        })
        batch["状态"] = "存在待核对项" if reviews else "已完成"
        batch["updated_at"] = _now()

    def _write_check(self, batch: dict[str, Any], change: dict[str, Any]) -> None:
        checks = store.rows(CHECK_MODULE)
        existing = next((row for row in checks if _text(row.get("批次号")) == batch["批次号"]), None)
        stats = batch["统计"]
        boundary_conflicts = batch["生效边界"]["冲突行数"]
        result = "通过"
        if stats["两列冲突"]:
            result = "存在待核对项"
        elif boundary_conflicts:
            result = "存在生效边界冲突"

        items = [
            {"核对项": "字段改名映射", "结果": "通过", "说明": f"{batch['旧字段']} -> {batch['新字段']}"},
            {
                "核对项": "两列金额冲突",
                "结果": "通过" if not stats["两列冲突"] else "需核对",
                "说明": f"{stats['两列冲突']} 行已进入待核对区，聚合已跳过",
            },
            {
                "核对项": "生效边界",
                "结果": "通过" if not boundary_conflicts else "需关注",
                "说明": (
                    f"{boundary_conflicts} 行位于生效边界之外"
                    if boundary_conflicts else
                    ("已声明边界" if batch["生效边界"]["边界值"] is not None else "未声明生效边界")
                ),
            },
            {
                "核对项": "重复写入",
                "结果": "通过",
                "说明": f"每行仅写入一次；重复跳过 {stats['重复跳过']} 行",
            },
            {
                "核对项": "回滚追溯",
                "结果": "通过",
                "说明": f"已保留原行标识与原列映射，可按旧列 {batch['旧字段']} 回滚",
            },
        ]
        payload = {
            "核对编号": existing["核对编号"] if existing else f"CHK-{len(checks) + 1:04d}",
            "批次号": batch["批次号"],
            "变更ID": change.get("id"),
            "变更编号": change.get("变更编号"),
            "关联流": change.get("关联流"),
            "旧字段": batch["旧字段"],
            "新字段": batch["新字段"],
            "核对结果": result,
            "校验项": items,
            "转换行数": stats["转换行数"],
            "待核对行数": stats["两列冲突"],
            "生效边界冲突行数": boundary_conflicts,
            "写入次数": stats["已确认金额写入次数"],
            "字段版本依赖": copy.deepcopy(batch["字段版本依赖"]),
            "数据库写入范围": copy.deepcopy(batch["数据库写入范围"]),
            "updated_at": _now(),
        }
        if existing is None:
            payload["id"] = _next_id(checks)
            payload["created_at"] = _now()
            checks.append(payload)
        else:
            existing.update(payload)

        # 回写结构核对清单到结构变更记录本身，页面读取单条变更即可看到结论。
        change["核对结果"] = result
        change["兼容批次"] = batch["批次号"]
        change["待核对行数"] = stats["两列冲突"]
        change["核对时间"] = payload["updated_at"]
        change["字段版本依赖"] = copy.deepcopy(batch["字段版本依赖"])
        change["数据库写入范围"] = copy.deepcopy(batch["数据库写入范围"])

    def _batch_summary(self, batch: dict[str, Any]) -> dict[str, Any]:
        stats = batch.get("统计", {})
        return {
            "id": batch["id"],
            "批次号": batch["批次号"],
            "变更ID": batch.get("变更ID"),
            "变更编号": batch.get("变更编号"),
            "关联流": batch.get("关联流"),
            "旧字段": batch["旧字段"],
            "新字段": batch["新字段"],
            "旧版本": batch["旧版本"],
            "新版本": batch["新版本"],
            "状态": batch.get("状态"),
            "转换行数": stats.get("转换行数", 0),
            "两列一致": stats.get("两列一致", 0),
            "纯旧列": stats.get("纯旧列", 0),
            "纯新列": stats.get("纯新列", 0),
            "待核对行数": stats.get("两列冲突", 0),
            "旧版总金额": stats.get("旧版总金额"),
            "新版总金额": stats.get("新版总金额"),
            "生效边界冲突行数": batch.get("生效边界", {}).get("冲突行数", 0),
            "数据库写入范围": copy.deepcopy(batch.get("数据库写入范围", {})),
            "updated_at": batch.get("updated_at"),
        }
