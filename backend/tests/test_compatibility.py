"""兼容转换测试：覆盖纯旧列、纯新列、两列一致、两列冲突与迁移中断续做。"""
from app.services.compatibility import CompatibilityService
from app.store import store


def _service() -> CompatibilityService:
    return CompatibilityService()


class TestResolveRename:
    def test_rename_change_is_resolved(self):
        service = _service()
        change, error = service.resolve_change("SCHE-0002")
        assert error == ""
        assert service.resolve_rename(change) == ("金额", "实付金额", "")

    def test_non_rename_change_is_rejected(self):
        service = _service()
        change, _ = service.resolve_change(1)
        old, new, error = service.resolve_rename(change)
        assert (old, new) == ("", "")
        assert "只有字段改名" in error

    def test_missing_mapping_is_explained(self):
        change = {"id": 99, "变更编号": "SCHE-0099", "变更类型": "字段改名", "删除字段": "", "新增字段": ""}
        _, _, error = _service().resolve_rename(change)
        assert "缺少改名映射" in error


class TestFourInputShapes:
    def test_input_shapes_are_classified(self):
        batch, message = _service().run(change_id=2)
        assert batch is not None, message
        kinds = {}
        for record in batch["records"]:
            kinds[record["输入形态"]] = kinds.get(record["输入形态"], 0) + 1
        assert kinds == {"纯旧列": 3, "纯新列": 3, "两列一致": 2}
        assert batch["统计"]["两列冲突"] == 2

    def test_both_views_are_readable(self):
        batch, _ = _service().run(change_id=2)
        for record in batch["records"]:
            assert record["旧版视图"]["字段"] == "金额"
            assert record["新版视图"]["字段"] == "实付金额"
            assert record["旧版视图"]["金额"] == record["新版视图"]["金额"]
            assert record["旧版视图"]["字段版本"] != record["新版视图"]["字段版本"]

    def test_legacy_view_keeps_original_value(self):
        batch, _ = _service().run(change_id=2)
        record = next(r for r in batch["records"] if r["原行标识"] == "ORD-102")
        # 纯新列行：旧版视图是推导值，但来源列如实标注为新列。
        assert record["旧版视图"]["推导"] is True
        assert record["旧版视图"]["来源列"] == "实付金额"
        record = next(r for r in batch["records"] if r["原行标识"] == "ORD-101")
        assert record["旧版视图"]["推导"] is False
        assert record["旧版视图"]["来源列"] == "金额"

    def test_conflicts_are_quarantined_by_row_key(self):
        batch, _ = _service().run(change_id=2)
        review_keys = {row["原行标识"] for row in _service().list_reviews(batch_no=batch["批次号"])}
        assert review_keys == {"ORD-104", "ORD-108"}
        assert all(row["状态"] == "待核对" for row in _service().list_reviews())
        written = {record["原行标识"] for record in batch["records"]}
        assert not (review_keys & written)

    def test_aggregation_skips_conflicts(self):
        batch, _ = _service().run(change_id=2)
        assert batch["聚合"]["行数"] == 8
        assert batch["聚合"]["跳过争议行数"] == 2
        # 128.5+56+320+250+175+42.5+198+67.8
        assert batch["聚合"]["金额合计"] == 1237.8

    def test_unparseable_value_is_rejected_not_aggregated(self):
        batch, message = _service().run(change_id=2, records=[
            {"订单编号": "ORD-201", "金额": "待定"},
            {"订单编号": "ORD-202", "金额": 10.0},
        ])
        assert batch is not None, message
        assert batch["统计"]["拒绝行数"] == 1
        assert batch["统计"]["转换行数"] == 1
        assert batch["聚合"]["金额合计"] == 10.0


class TestIdempotentResume:
    def test_resume_skips_processed_rows(self):
        service = _service()
        batch, _ = service.run(change_id=2)
        batch_no = batch["批次号"]
        _, message = service.run(change_id=2, batch_no=batch_no, records=[
            {"订单编号": "ORD-101", "金额": 128.5},
            {"订单编号": "ORD-111", "实付金额": 33.0},
        ])
        assert "续做完成" in message
        resumed = service.find_batch_by_no(batch_no)
        assert resumed["统计"]["转换行数"] == 9
        assert resumed["统计"]["重复跳过"] == 1
        assert resumed["统计"]["已确认金额写入次数"] == 9
        assert all(record["写入次数"] == 1 for record in resumed["records"])

    def test_same_batch_twice_writes_each_row_once(self):
        service = _service()
        batch, _ = service.run(change_id=2)
        service.run(change_id=2, batch_no=batch["批次号"])
        resumed = service.find_batch_by_no(batch["批次号"])
        keys = [record["原行标识"] for record in resumed["records"]]
        assert len(keys) == len(set(keys)) == 8

    def test_resume_rejects_other_change_mapping(self):
        service = _service()
        batch, _ = service.run(change_id=2)
        store.find("schema_evolve", 2)["删除字段"] = "订单金额"
        store.find("schema_evolve", 2)["新增字段"] = "结算金额"
        result, message = service.run(change_id=2, batch_no=batch["批次号"])
        assert result is None
        assert "字段映射" in message

    def test_rolled_back_change_cannot_run(self):
        store.find("schema_evolve", 2)["status"] = "已回滚"
        batch, message = _service().run(change_id=2)
        assert batch is None
        assert "已回滚" in message

    def test_rejected_row_can_be_reprocessed_on_resume(self):
        service = _service()
        batch, _ = service.run(change_id=2, records=[{"订单编号": "ORD-301", "金额": "待定"}])
        assert batch["统计"]["拒绝行数"] == 1
        resumed, _ = service.run(change_id=2, batch_no=batch["批次号"],
                                 records=[{"订单编号": "ORD-301", "金额": 15.0}])
        assert resumed["统计"]["拒绝行数"] == 0
        assert resumed["统计"]["转换行数"] == 1
        assert resumed["拒绝行"] == []

    def test_new_batch_gets_suffix_when_number_taken(self):
        service = _service()
        first, _ = service.run(change_id=2)
        second, _ = service.run(change_id=2)
        assert first["批次号"] == "COMPAT-SCHE-0002"
        assert second["批次号"] == "COMPAT-SCHE-0002-02"


class TestEffectiveBoundary:
    def test_boundary_conflicts_are_reported(self):
        batch, _ = _service().run(change_id=2, effective_boundary=500, old_pipeline_backfill=True)
        conflicts = batch["生效边界"]["冲突明细"]
        assert {row["原行标识"] for row in conflicts} == {"ORD-102", "ORD-109"}
        assert batch["生效边界"]["边界状态"].startswith("旧流水线补算中")
        assert batch["统计"]["转换行数"] == 8

    def test_boundary_dependency_visible_per_downstream_version(self):
        batch, _ = _service().run(change_id=2, effective_boundary=500)
        deps = {row["下游版本"]: row for row in batch["字段版本依赖"]}
        assert deps["v1"]["读取字段"] == "金额"
        assert deps["v2"]["读取字段"] == "实付金额"
        assert deps["v1"]["可读行数"] == deps["v2"]["可读行数"] == 8

    def test_database_write_scope_declared(self):
        batch, _ = _service().run(change_id=2)
        scope = batch["数据库写入范围"]
        assert scope["写入字段"] == ["实付金额", "金额"]
        assert scope["写入行数"] == 8
        assert scope["位点范围"] == [100, 1000]
        assert scope["每行写入次数"] == 1


class TestChecklistWriteBack:
    def test_check_written_to_schema_module(self):
        batch, _ = _service().run(change_id=2, effective_boundary=500)
        checks = _service().list_checks(change_id=2)
        assert len(checks) == 1
        check = checks[0]
        assert check["批次号"] == batch["批次号"]
        assert check["核对结果"] == "存在待核对项"
        assert check["待核对行数"] == 2
        assert any(item["核对项"] == "回滚追溯" and item["结果"] == "通过" for item in check["校验项"])

        change = store.find("schema_evolve", 2)
        assert change["核对结果"] == "存在待核对项"
        assert change["兼容批次"] == batch["批次号"]
        assert change["待核对行数"] == 2

    def test_check_is_updated_not_duplicated_on_resume(self):
        service = _service()
        batch, _ = service.run(change_id=2)
        service.run(change_id=2, batch_no=batch["批次号"])
        assert len(service.list_checks(change_id=2)) == 1


class TestRollbackTrace:
    def test_trace_maps_back_to_original_column(self):
        service = _service()
        batch, _ = service.run(change_id=2)
        trace, message = service.rollback_trace(batch["id"])
        assert trace is not None, message
        assert trace["回滚映射"]["回滚后读取字段"] == "金额"
        assert len(trace["行追溯"]) == 8
        assert len(trace["争议行"]) == 2
        pure_new = next(row for row in trace["行追溯"] if row["原行标识"] == "ORD-102")
        assert pure_new["原列"] == "实付金额"
        assert pure_new["回滚写入"] == {"金额": 56.0}

    def test_missing_batch_returns_message(self):
        trace, message = _service().rollback_trace(999)
        assert trace is None
        assert "不存在" in message
