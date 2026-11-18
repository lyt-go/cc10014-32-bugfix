"""发布快照版本回归测试。

覆盖的核心缺陷与约束：
- 转换节点再次执行后，历史发布快照的行数与查询字段不变，新结果只形成新版本。
- 发布动作绑定同一完整批次；半成品/部分行/实时表都不能被发布消费。
- 发布快照版本归属冲突拒绝；重复提交同一完整批次只生效一次。
- 转换完成事件与发布请求并发时，先落地的差异决定结果。
- 导出/下载/打包/下载书签、跨模块持久化与文件交接。
"""
from __future__ import annotations

import io
import json
import zipfile

import pytest

from app.store import store
from app.services.data_publish import DataPublishService
from app.services.transform_dag import TransformDagService


DWD_ROWS = [
    {"order_id": "O001", "order_amount": "100", "order_status": "已完成", "dt": "2026-11-01"},
    {"order_id": "O002", "order_amount": "200", "order_status": "已完成", "dt": "2026-11-01"},
    {"order_id": "O003", "order_amount": "50", "order_status": "待支付", "dt": "2026-11-02"},
]


def setup_function(_):
    store.reset()


@pytest.fixture(autouse=True)
def _fresh_store():
    store.reset()
    yield
    store.reset()


def _run_transform(entry_id: int = 1) -> dict:
    return TransformDagService().execute_node(entry_id)


def _publish(entry_id: int = 1) -> tuple[dict | None, str]:
    return DataPublishService().run_action(entry_id, "确认发布")


class TestPublishSnapshotFreeze:
    def test_historical_snapshot_survives_second_transform_run(self):
        """再次执行转换后历史发布快照行数不变，新结果只形成新的发布版本。"""
        store.set_table_rows("dwd_order_detail", DWD_ROWS)
        _run_transform()
        entry, message = _publish()
        assert entry is not None, message
        assert entry["发布快照版本"] == "v1"

        first = DataPublishService().get_published_data(1, 1)
        assert first is not None
        assert first["row_count"] == 2
        assert first["fields"] == ["dt", "order_status"]

        # 新数据到达后再次执行转换：实时表被改写，但历史快照必须原样可读。
        store.set_table_rows("dwd_order_detail", DWD_ROWS + [
            {"order_id": "O004", "order_amount": "300", "order_status": "已完成", "dt": "2026-11-03"},
        ])
        _run_transform()
        assert len(store.table_rows("dws_order_daily")) == 3

        again = DataPublishService().get_published_data(1, 1)
        assert again is not None
        assert again["row_count"] == 2
        assert len(again["rows"]) == 2
        assert again["fields"] == ["dt", "order_status"]

        entry2, message2 = _publish()
        assert entry2 is not None, message2
        assert entry2["发布快照版本"] == "v2"
        versions = DataPublishService().list_versions(1)
        assert [item["version"] for item in versions] == ["v1", "v2"]
        assert [item["row_count"] for item in versions] == [2, 3]
        assert all(item["fields"] == ["dt", "order_status"] for item in versions)

    def test_published_data_does_not_reference_live_table(self):
        """发布后直接改写实时表（不经转换）也不能影响已发布快照。"""
        store.set_table_rows("dwd_order_detail", DWD_ROWS)
        _run_transform()
        _publish()
        store.set_table_rows("dws_order_daily", [{"dt": "2099-01-01", "order_status": "篡改"}])
        data = DataPublishService().get_published_data(1)
        assert data is not None
        assert data["row_count"] == 2
        assert all(row.get("dt") != "2099-01-01" for row in data["rows"])

    def test_publish_without_snapshot_returns_no_data(self):
        """未发布过的版本没有发布快照，读取必须为空而不是读取实时表。"""
        store.set_table_rows("dws_order_daily", [{"dt": "2026-11-01"}])
        assert DataPublishService().get_published_data(1) is None


class TestCompleteBatchBinding:
    def test_publish_requires_existing_batch(self):
        """来源转换节点尚未执行时，发布被阻断。"""
        entry, message = _publish()
        assert entry is None
        assert "尚无转换批次" in message

    def test_partial_batch_rejected(self):
        """批次只复制了部分行时视为半成品，拒绝发布。"""
        store.set_table_rows("dwd_order_detail", DWD_ROWS)
        _run_transform()
        batch = store.latest_transform_batch("TRAN-ORD-001")
        assert batch is not None
        store.save_transform_batch({**batch, "rows": batch["rows"][:1]})
        entry, message = _publish()
        assert entry is None
        assert "尚未完整落盘" in message or "完整" in message

    def test_batch_without_integrity_flag_rejected(self):
        store.set_table_rows("dwd_order_detail", DWD_ROWS)
        _run_transform()
        batch = store.latest_transform_batch("TRAN-ORD-001")
        store.save_transform_batch({**batch, "integrity": False})
        entry, _ = _publish()
        assert entry is None

    def test_failed_batch_not_published(self):
        store.set_table_rows("dwd_order_detail", DWD_ROWS)
        _run_transform()
        batch = store.latest_transform_batch("TRAN-ORD-001")
        store.save_transform_batch({**batch, "status": "失败", "integrity": False})
        entry, _ = _publish()
        assert entry is None

    def test_snapshot_matches_batch_row_count(self):
        """发布快照行数与绑定批次行数、冻结内容三者一致。"""
        store.set_table_rows("dwd_order_detail", DWD_ROWS)
        result = _run_transform()
        entry, message = _publish()
        assert entry is not None, message
        batch = store.get_transform_batch(result["batch_no"])
        data = DataPublishService().get_published_data(1)
        assert batch is not None and data is not None
        assert data["batch_no"] == str(batch["batch_no"])
        assert data["row_count"] == batch["row_count"] == len(data["rows"])
        assert entry.get("发布快照行数") == len(data["rows"])


class TestIdempotencyAndConflict:
    def test_repeat_publish_same_batch_effective_once(self):
        """同一完整批次重复提交发布只生效一次。"""
        store.set_table_rows("dwd_order_detail", DWD_ROWS)
        _run_transform()
        first, msg1 = _publish()
        assert first is not None, msg1
        second, msg2 = _publish()
        assert second is not None, msg2
        assert "重复提交未生成新版本" in msg2
        assert len(DataPublishService().list_versions(1)) == 1
        assert store.max_publish_version(1) == 1

    def test_version_ownership_conflict_rejected(self):
        """发布快照版本归属冲突即拒绝：同一版本编号不能归属另一个发布版本。"""
        store.set_table_rows("dwd_order_detail", DWD_ROWS)
        _run_transform()
        assert _publish(1)[0] is not None
        # 另一个发布版本冒用同一版本编号
        dp = DataPublishService()
        dp.create_entry({
            "版本编号": "PUB-ORD-001",
            "数据集名称": "dws_order_daily",
            "发布范围": "外部",
            "查询参数": "dt",
            "响应字段": "dt",
            "缓存秒数": "60",
            "分页上限": "100",
            "消费应用": "外部应用",
            "来源转换节点": "TRAN-ORD-001",
        })
        entry, message = _publish(4)
        assert entry is None
        assert "归属冲突" in message
        assert len(dp.list_versions(4)) == 0

    def test_withdraw_keeps_snapshot(self):
        """撤回发布版本不删除历史发布快照版本。"""
        store.set_table_rows("dwd_order_detail", DWD_ROWS)
        _run_transform()
        _publish()
        entry, message = DataPublishService().run_action(1, "撤回版本")
        assert entry is not None, message
        assert entry["status"] == "已撤回"
        assert len(DataPublishService().list_versions(1)) == 1
        assert DataPublishService().get_published_data(1, 1)["row_count"] == 2


class TestConcurrentFirstDifferenceWins:
    def test_complete_batch_wins_shows_new_rows_and_batch(self):
        """转换完成先落地：发布显示新汇总行数与转换批次。"""
        store.set_table_rows("dwd_order_detail", DWD_ROWS)
        result = _run_transform()
        entry, message = _publish()
        assert entry is not None, message
        assert entry["发布快照行数"] == 2
        assert entry["转换批次"] == str(result["batch_no"])

    def test_pending_batch_keeps_previous_complete_result(self):
        """发布请求先落地且批次未完成：保留上一完整结果，并记录待发布转换批次。"""
        store.set_table_rows("dwd_order_detail", DWD_ROWS)
        _run_transform()
        _publish()
        previous = DataPublishService().get_published_data(1, 1)

        # 新批次已开始但尚未完整落盘
        pending_no = store.next_transform_batch("TRAN-ORD-001")
        store.save_transform_batch({
            "batch_no": pending_no, "node_code": "TRAN-ORD-001",
            "input_table": "dwd_order_detail", "output_table": "dws_order_daily",
            "status": "执行中", "integrity": False, "row_count": 0, "rows": [],
        })
        entry, message = _publish()
        assert entry is not None, message
        assert "上一完整结果" in message
        assert entry["发布快照版本"] == "v1"
        assert entry["待发布转换批次"] == str(pending_no)
        assert DataPublishService().get_published_data(1)["rows"] == previous["rows"]

        # 批次完成后再次发布：形成新版本，待发布标记清空
        store.save_transform_batch({
            "batch_no": pending_no, "node_code": "TRAN-ORD-001",
            "input_table": "dwd_order_detail", "output_table": "dws_order_daily",
            "status": "已完成", "integrity": True, "row_count": 1,
            "rows": [{"dt": "2026-11-02", "order_status": "待支付", "order_amount_agg": 50.0}],
        })
        entry2, message2 = _publish()
        assert entry2 is not None, message2
        assert entry2["发布快照版本"] == "v2"
        assert entry2["转换批次"] == str(pending_no)
        assert entry2["待发布转换批次"] == ""


class TestExportDownloadPackage:
    def test_export_csv_matches_frozen_rows_and_fields(self):
        store.set_table_rows("dwd_order_detail", DWD_ROWS)
        _run_transform()
        _publish()
        exported = DataPublishService().export_version_csv(1, 1)
        assert exported is not None
        text, filename = exported
        lines = [line for line in text.strip().splitlines() if line]
        assert lines[0].lstrip("﻿") == "dt,order_status"
        assert len(lines) == 3  # 表头 + 2 行
        assert filename.endswith("v1.csv")

    def test_package_contains_csv_and_manifest(self):
        store.set_table_rows("dwd_order_detail", DWD_ROWS)
        _run_transform()
        _publish()
        packaged = DataPublishService().package_version(1, 1)
        assert packaged is not None
        payload, filename = packaged
        assert filename.endswith("v1.zip")
        with zipfile.ZipFile(io.BytesIO(payload)) as zf:
            names = zf.namelist()
            assert any(name.endswith(".csv") for name in names)
            manifest = json.loads(zf.read("manifest.json"))
        assert manifest["row_count"] == 2
        assert manifest["fields"] == ["dt", "order_status"]
        assert manifest["batch_no"]

    def test_bookmark_downloads_frozen_version(self):
        store.set_table_rows("dwd_order_detail", DWD_ROWS)
        _run_transform()
        _publish()
        dp = DataPublishService()
        bookmark = dp.save_download_bookmark(1, 1, "csv")
        assert bookmark is not None
        # 转换再次执行后书签仍指向原版本
        store.set_table_rows("dwd_order_detail", DWD_ROWS + [
            {"order_id": "O009", "order_amount": "10", "order_status": "已完成", "dt": "2026-11-09"},
        ])
        _run_transform()
        resolved = dp.resolve_download_bookmark(bookmark["token"])
        assert resolved is not None
        payload, _, info = resolved
        assert info["version_no"] == 1
        assert len([l for l in payload.strip().splitlines() if l]) == 3


class TestCrossModuleAndHandoff:
    def test_transform_detail_exposes_publish_versions(self):
        """转换编排详情页能读取数据发布模块持久化的发布快照版本。"""
        store.set_table_rows("dwd_order_detail", DWD_ROWS)
        _run_transform()
        _publish()
        view = TransformDagService().get_publish_versions(1)
        assert view["ok"] is True
        assert len(view["items"]) == 1
        assert view["items"][0]["version"] == "v1"
        assert view["items"][0]["发布版本id"] == 1
        assert len(view["batches"]) == 1

    def test_list_page_and_transform_detail_agree(self):
        """列表页与转换编排详情页对同一发布版本显示同一份快照口径。"""
        store.set_table_rows("dwd_order_detail", DWD_ROWS)
        _run_transform()
        _publish()
        listed, _ = DataPublishService().list_entries(keyword="PUB-ORD-001")
        detail = TransformDagService().get_publish_versions(1)["items"][0]
        assert listed[0]["发布快照版本"] == detail["version"]
        assert listed[0]["发布快照行数"] == detail["row_count"]
        assert listed[0]["转换批次"] == detail["batch_no"]

    def test_stale_entry_fields_recovered_from_snapshot(self):
        """同源恢复缺口：条目上残留的旧行数不能与快照版本重复显示成两份结果。"""
        store.set_table_rows("dwd_order_detail", DWD_ROWS)
        _run_transform()
        _publish()
        entry = store.find("data_publish", 1)
        # 模拟恢复后条目字段与快照不一致
        entry["发布快照行数"] = 999
        entry["发布快照版本"] = "v0"
        listed, _ = DataPublishService().list_entries(keyword="PUB-ORD-001")
        detail = DataPublishService().decorate(entry)
        assert listed[0]["发布快照行数"] == 2
        assert detail["发布快照行数"] == 2
        assert listed[0]["发布快照版本"] == "v1"
        assert listed[0]["发布快照状态"] == "已冻结"

    def test_published_without_snapshot_is_flagged(self):
        """已发布却没有快照的条目要显式标成缺失，而不是回落到实时表。"""
        entry = store.find("data_publish", 1)
        entry["status"] = "已发布"
        listed, _ = DataPublishService().list_entries(keyword="PUB-ORD-001")
        assert listed[0]["发布快照状态"] == "缺失"
        assert DataPublishService().get_published_data(1) is None

    def test_version_order_is_deterministic(self):
        """发布版本次序按版本号单调递增，每次发布只前进一格。"""
        store.set_table_rows("dwd_order_detail", DWD_ROWS)
        _run_transform()
        _publish()
        for extra in (1, 2, 3):
            store.set_table_rows("dwd_order_detail", DWD_ROWS + [
                {"order_id": f"OX{extra}", "order_amount": "1", "order_status": "已完成", "dt": f"2026-11-0{extra}"},
            ])
            _run_transform()
            assert _publish()[0] is not None
        versions = DataPublishService().list_versions(1)
        assert [item["version"] for item in versions] == ["v1", "v2", "v3", "v4"]
        assert [item["version_no"] for item in versions] == [1, 2, 3, 4]

    def test_handoff_follows_call_chain(self):
        store.set_table_rows("dwd_order_detail", DWD_ROWS)
        result = _run_transform()
        _publish()
        handoff = DataPublishService().handoff(1, 1)
        assert handoff is not None
        assert handoff["发布版本"] == "PUB-ORD-001"
        assert handoff["调用链"]["节点编号"] == "TRAN-ORD-001"
        assert result["batch_no"] in handoff["调用链"]["转换批次"]
        assert handoff["完整版本"][0]["complete"] is True
        assert handoff["完整版本"][0]["row_count"] == 2

    def test_handoff_rejects_missing_version(self):
        store.set_table_rows("dwd_order_detail", DWD_ROWS)
        _run_transform()
        _publish()
        assert DataPublishService().handoff(1, 99) is None
