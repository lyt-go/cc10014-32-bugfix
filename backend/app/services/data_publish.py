"""发布版本业务规则：把转换批次的完整输出冻结为发布快照版本，供下游消费与追溯。

关键约束：
- 发布动作只消费「已完整落盘」的转换批次，绝不引用实时表；快照行数必须与批次行数一致。
- 每个发布快照版本冻结发布当时的行数与查询字段，之后转换再次执行不会改写历史版本。
- 版本号归属于发布版本；归属冲突直接拒绝，重复提交同一完整批次只生效一次。
- 转换完成与发布请求并发时，先落地的差异决定结果：批次完整则发布新版本，
  批次未完成则保留上一完整结果并记录待发布转换批次。
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import secrets
import zipfile
from datetime import datetime
from typing import Any

from app.store import store

MODULE = "data_publish"
LIST_FIELDS = ["版本编号", "数据集名称", "发布范围", "查询参数", "响应字段", "缓存秒数", "分页上限", "消费应用"]
REQUIRED_FIELDS = ["版本编号", "数据集名称"]
STATUS_ORDER = ["草稿", "待校验", "已发布", "已撤回"]
ACTION_RULES = {"提交校验": "待校验", "确认发布": "已发布", "撤回版本": "已撤回", "标记下线": "已下线"}
NEGATIVE_ACTIONS = ["撤回版本", "标记下线"]
TERMINAL_STATUSES = {"已撤回", "已下线"}


def _clean_value(value: Any) -> str:
    text = "" if value is None else str(value)
    if text.startswith(("=", "+", "-", "@")):
        return "'" + text
    return text


class DataPublishService:
    def list_entries(
        self,
        *,
        filters: dict[str, str] | None = None,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        for field, expected in (filters or {}).items():
            rows = [row for row in rows if expected.casefold() in str(row.get(field, "")).casefold()]
        if keyword:
            rows = [row for row in rows if keyword.casefold() in str(row.get("版本编号", "")).casefold()]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = (page - 1) * size
        return [self.decorate(row) for row in rows[start:start + size]], total

    def decorate(self, entry: dict[str, Any]) -> dict[str, Any]:
        """列表页与转换编排详情页共用同一份发布快照口径，避免同源数据重复展示。

        以发布快照存储为准回填条目字段：恢复后条目上的旧字段可能与实际冻结
        版本对不上，这里统一按快照重新计算，两处读到的永远是一份口径。
        """
        item = dict(entry)
        entry_id = int(entry.get("id", 0))
        snapshots = store.list_publish_snapshots(entry_id)
        latest = snapshots[-1] if snapshots else None
        if latest is not None:
            item["发布快照版本"] = f"v{latest.get('version_no')}"
            item["发布快照行数"] = latest.get("row_count", 0)
            item["发布快照时间"] = latest.get("frozen_at", "")
            item["转换批次"] = latest.get("batch_no", "")
            item["发布查询字段"] = ",".join(latest.get("fields", []))
            item["发布快照状态"] = "已冻结"
        else:
            item.setdefault("发布快照版本", "")
            item.setdefault("发布快照行数", 0)
            item.setdefault("转换批次", "")
            item["发布快照状态"] = "缺失" if entry.get("status") == "已发布" else "未冻结"
        item["待发布转换批次"] = entry.get("待发布转换批次", "")
        item["发布快照版本数"] = len(snapshots)
        return item

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in LIST_FIELDS})
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        if values.get("来源转换节点"):
            entry["来源转换节点"] = values["来源转换节点"]
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"发布版本 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于数据发布可执行范围"
        if action == "确认发布":
            return self._confirm_publish(entry)
        target = ACTION_RULES[action]
        entry["status"] = target
        entry["pending"] = target not in TERMINAL_STATUSES
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        # 撤回/下线只改变发布状态，历史发布快照版本保持原样可追溯。
        store.save_version(MODULE, entry_id, entry)
        return entry, f"发布版本已{action}"

    # ------------------------------------------------------------------
    # 确认发布：冻结完整批次，生成唯一发布快照版本
    # ------------------------------------------------------------------
    def _confirm_publish(self, entry: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        entry_id = int(entry["id"])
        node = self._resolve_node(entry)
        if entry.get("来源转换节点") and node is None:
            return None, f"数据发布被阻断：来源转换节点 {entry.get('来源转换节点')} 不存在或已归档"
        batch: dict[str, Any] | None = None
        if node is not None:
            batches = store.list_transform_batches(node.get("节点编号", ""))
            batch = batches[-1] if batches else None
            if batch is None:
                return None, (
                    f"数据发布被阻断：来源转换节点 {node.get('节点编号')} 尚无转换批次，"
                    "发布快照必须绑定完整转换输出"
                )
            if not self._batch_complete(batch):
                # 转换仍在执行：先落地的差异是「批次未完成」，发布保留上一完整结果。
                entry["待发布转换批次"] = str(batch.get("batch_no", ""))
                entry["pending"] = True
                store.save_version(MODULE, entry_id, entry)
                previous = self._latest_snapshot(entry_id)
                if previous is not None:
                    return entry, (
                        f"转换批次 {batch.get('batch_no')} 尚未完整落盘，"
                        f"发布版本保持上一完整结果 v{previous['version_no']}"
                    )
                return None, (
                    f"数据发布被阻断：转换批次 {batch.get('batch_no')} 尚未完整落盘，"
                    "没有可用的完整发布结果"
                )

        rows = [dict(row) for row in (batch or {}).get("rows", [])] if batch else self._live_rows(entry)
        fields = self._query_fields(entry, rows)
        fingerprint = self._fingerprint(rows, fields)
        batch_no = str((batch or {}).get("batch_no", "")) or "MANUAL"

        # 重复提交同一完整批次：只生效一次，不重复占用版本号。
        # 注意只认「同一批次」；转换再次执行形成的新批次即使行内容相同也生成新版本。
        reusable = self._find_snapshot_for_replay(entry_id, batch_no, fingerprint)
        if reusable is not None:
            entry["status"] = "已发布"
            entry["pending"] = False
            entry["abnormal"] = False
            entry["待发布转换批次"] = ""
            self._sync_entry_snapshot_fields(entry, reusable)
            store.save_version(MODULE, entry_id, entry)
            return entry, f"发布快照版本 v{reusable['version_no']} 已生效，重复提交未生成新版本"

        # 质量稽核作用在即将冻结的完整批次上，而不是实时表。
        dataset = entry.get("数据集名称", "")
        qc_result = self._check_quality(dataset, rows)
        if not qc_result.get("passed", False):
            return None, f"数据发布被阻断：{qc_result.get('message', '质量稽核未通过')}"

        # 发布快照版本归属冲突：同一版本编号已被别的发布版本占用时必须拒绝，
        # 否则两个发布版本会共用一个版本号，下游文件交接无法定位。
        entry_code = entry.get("版本编号", "")
        for existing in store.list_all_publish_snapshots():
            if existing.get("entry_code") == entry_code and int(existing.get("publish_entry_id", 0)) != entry_id:
                return None, (
                    f"发布快照版本归属冲突：版本编号 {entry_code} 已归属发布版本 "
                    f"{existing.get('publish_entry_id')}，当前发布版本不可覆盖"
                )

        version_no = store.max_publish_version(entry_id) + 1
        snapshot = {
            "version": f"v{version_no}",
            "entry_code": entry.get("版本编号", ""),
            "dataset": dataset,
            "table": (batch or {}).get("output_table") or self._resolve_table(entry, node),
            "node_code": (node or {}).get("节点编号", entry.get("来源转换节点", "")),
            "batch_no": batch_no,
            "rows": rows,
            "row_count": len(rows),
            "fields": fields,
            "fingerprint": fingerprint,
            "source": "转换批次" if batch else "发布时冻结",
            "frozen_at": datetime.now().isoformat(),
            "status": "已发布",
        }
        try:
            store.save_publish_snapshot(entry_id, version_no, snapshot)
        except ValueError:
            return None, (
                f"发布快照版本归属冲突：发布版本 {entry_id} 的 v{version_no} 已被占用，"
                "请核对转换批次后重试"
            )

        entry["status"] = "已发布"
        entry["pending"] = False
        entry["abnormal"] = False
        entry["待发布转换批次"] = ""
        self._sync_entry_snapshot_fields(entry, snapshot)
        store.save_version(MODULE, entry_id, entry)
        return entry, f"发布版本已确认发布，生成发布快照版本 v{version_no}（{len(rows)}行）"

    def _sync_entry_snapshot_fields(self, entry: dict[str, Any], snapshot: dict[str, Any]) -> None:
        entry["发布快照版本"] = snapshot.get("version", "")
        entry["发布快照行数"] = snapshot.get("row_count", 0)
        entry["转换批次"] = snapshot.get("batch_no", "")
        entry["发布查询字段"] = ",".join(snapshot.get("fields", []))

    def _batch_complete(self, batch: dict[str, Any]) -> bool:
        if batch.get("status") != "已完成" or not batch.get("integrity"):
            return False
        rows = batch.get("rows")
        if not isinstance(rows, list):
            return False
        # 只复制了部分行同样视为半成品。
        return len(rows) == int(batch.get("row_count", -1))

    def _live_rows(self, entry: dict[str, Any]) -> list[dict[str, Any]]:
        """未绑定转换节点的发布版本：发布时复制当前表数据，之后不再引用实时表。"""
        dataset = entry.get("数据集名称", "")
        return [dict(row) for row in store.table_rows(dataset)]

    def _resolve_node(self, entry: dict[str, Any]) -> dict[str, Any] | None:
        node_code = entry.get("来源转换节点", "")
        if node_code:
            return store.find_by_field("transform_dag", "节点编号", node_code)
        dataset = entry.get("数据集名称", "")
        if not dataset:
            return None
        return store.find_by_field("transform_dag", "输出表名", dataset)

    def _resolve_table(self, entry: dict[str, Any], node: dict[str, Any] | None) -> str:
        if node is not None:
            return node.get("输出表名", "")
        return entry.get("数据集名称", "")

    def _query_fields(self, entry: dict[str, Any], rows: list[dict[str, Any]]) -> list[str]:
        declared = [
            field.strip()
            for field in str(entry.get("查询参数", "")).split(",")
            if field.strip()
        ]
        if declared:
            return declared
        keys: list[str] = []
        for row in rows:
            for key in row:
                if key not in keys:
                    keys.append(key)
        return keys

    def _fingerprint(self, rows: list[dict[str, Any]], fields: list[str]) -> str:
        payload = json.dumps({"fields": fields, "rows": rows}, ensure_ascii=False, sort_keys=True, default=str)
        return hashlib.sha1(payload.encode("utf-8")).hexdigest()

    def _find_snapshot_for_replay(self, entry_id: int, batch_no: str,
                                  fingerprint: str) -> dict[str, Any] | None:
        """重复提交判定：同批次 + 同内容才算重复提交，跨批次一律生成新版本。"""
        for snapshot in store.list_publish_snapshots(entry_id):
            if (snapshot.get("status") == "已发布"
                    and str(snapshot.get("batch_no")) == str(batch_no)
                    and snapshot.get("fingerprint") == fingerprint):
                return snapshot
        return None

    def _latest_snapshot(self, entry_id: int) -> dict[str, Any] | None:
        snapshots = store.list_publish_snapshots(entry_id)
        return snapshots[-1] if snapshots else None

    def _check_quality(self, dataset: str, rows: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        """检查数据集是否通过所有质量稽核规则；发布时校验即将冻结的批次行。"""
        from app.services.quality_check import QualityCheckService
        qc = QualityCheckService()
        rules = [
            row for row in store.rows("quality_check")
            if row.get("校验表名") == dataset and row.get("status") == "已生效"
        ]
        if not rules:
            return {"passed": True, "message": "无生效稽核规则，默认通过"}
        for rule in rules:
            result = qc.execute_check(rule["id"], rows=rows)
            res = result.get("result", {})
            if res.get("failed", 0) > 0:
                return {"passed": False, "message": f"规则 {rule.get('规则编号')} 稽核失败 {res.get('failed')} 条"}
        return {"passed": True, "message": f"全部 {len(rules)} 条稽核规则通过"}

    # ------------------------------------------------------------------
    # 发布快照版本读取
    # ------------------------------------------------------------------
    def list_versions(self, entry_id: int) -> list[dict[str, Any]]:
        return [self._public_snapshot(snapshot) for snapshot in store.list_publish_snapshots(entry_id)]

    def get_version(self, entry_id: int, version_no: int) -> dict[str, Any] | None:
        snapshot = store.get_publish_snapshot(entry_id, version_no)
        return self._public_snapshot(snapshot) if snapshot else None

    def _public_snapshot(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        item = dict(snapshot)
        rows = item.pop("rows", [])
        item["row_count"] = item.get("row_count", len(rows))
        item["fields"] = list(item.get("fields", []))
        return item

    def get_published_data(self, entry_id: int, version_no: int | None = None) -> dict[str, Any] | None:
        """只读取冻结的发布快照，不回落实时表；因此转换再次执行不会改写历史结果。"""
        entry = self.get_entry(entry_id)
        if entry is None:
            return None
        snapshot = self._select_snapshot(entry, version_no)
        if snapshot is None:
            return None
        return {
            "table": snapshot.get("table", ""),
            "rows": [dict(row) for row in snapshot.get("rows", [])],
            "row_count": snapshot.get("row_count", 0),
            "version": snapshot.get("version", ""),
            "version_no": snapshot.get("version_no", 0),
            "batch_no": snapshot.get("batch_no", ""),
            "fields": list(snapshot.get("fields", [])),
            "frozen_at": snapshot.get("frozen_at", ""),
            "source": snapshot.get("source", ""),
        }

    def _select_snapshot(self, entry: dict[str, Any], version_no: int | None) -> dict[str, Any] | None:
        if version_no is not None:
            return store.get_publish_snapshot(int(entry["id"]), version_no)
        return self._latest_snapshot(int(entry["id"]))

    # ------------------------------------------------------------------
    # 导出 / 下载文件 / 打包 / 下载书签
    # ------------------------------------------------------------------
    def export_version_csv(self, entry_id: int, version_no: int) -> tuple[str, str] | None:
        snapshot = store.get_publish_snapshot(entry_id, version_no)
        if snapshot is None:
            return None
        fields = list(snapshot.get("fields") or [])
        rows = snapshot.get("rows", [])
        if not fields and rows:
            for row in rows:
                for key in row:
                    if key not in fields:
                        fields.append(key)
        buf = io.StringIO()
        buf.write("﻿")
        writer = csv.writer(buf, lineterminator="\n")
        writer.writerow(fields)
        for row in rows:
            writer.writerow([_clean_value(row.get(field, "")) for field in fields])
        # 冻结口径校验：导出文件的行数必须与版本记录一致。
        if len(rows) != int(snapshot.get("row_count", len(rows))):
            return None
        filename = f"{snapshot.get('entry_code') or entry_id}-v{version_no}.csv"
        return buf.getvalue(), filename

    def package_version(self, entry_id: int, version_no: int) -> tuple[bytes, str] | None:
        snapshot = store.get_publish_snapshot(entry_id, version_no)
        if snapshot is None:
            return None
        exported = self.export_version_csv(entry_id, version_no)
        if exported is None:
            return None
        csv_text, csv_name = exported
        manifest = self._public_snapshot(snapshot)
        manifest["row_count"] = snapshot.get("row_count", 0)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(csv_name, csv_text)
            zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2, default=str))
        filename = f"{snapshot.get('entry_code') or entry_id}-v{version_no}.zip"
        return buf.getvalue(), filename

    def save_download_bookmark(self, entry_id: int, version_no: int, kind: str) -> dict[str, Any] | None:
        snapshot = store.get_publish_snapshot(entry_id, version_no)
        if snapshot is None:
            return None
        if kind not in ("csv", "package"):
            return None
        token = secrets.token_urlsafe(16)
        bookmark = {
            "token": token,
            "publish_entry_id": entry_id,
            "version_no": version_no,
            "version": snapshot.get("version", ""),
            "kind": kind,
            "row_count": snapshot.get("row_count", 0),
            "batch_no": snapshot.get("batch_no", ""),
            "created_at": datetime.now().isoformat(),
        }
        store.save_download_bookmark(token, bookmark)
        return bookmark

    def resolve_download_bookmark(self, token: str) -> tuple[bytes | str, str, dict[str, Any]] | None:
        bookmark = store.get_download_bookmark(token)
        if bookmark is None:
            return None
        entry_id = int(bookmark["publish_entry_id"])
        version_no = int(bookmark["version_no"])
        if bookmark["kind"] == "package":
            packaged = self.package_version(entry_id, version_no)
            if packaged is None:
                return None
            payload, filename = packaged
            return payload, filename, bookmark
        exported = self.export_version_csv(entry_id, version_no)
        if exported is None:
            return None
        text, filename = exported
        return text, filename, bookmark

    # ------------------------------------------------------------------
    # 文件交接：沿调用链定位发布版本结果
    # ------------------------------------------------------------------
    def handoff(self, entry_id: int, version_no: int | None = None) -> dict[str, Any] | None:
        entry = self.get_entry(entry_id)
        if entry is None:
            return None
        target = None
        if version_no is not None:
            target = store.get_publish_snapshot(entry_id, version_no)
            if target is None:
                return None
        node = self._resolve_node(entry)
        node_code = (node or {}).get("节点编号", entry.get("来源转换节点", ""))
        batches = store.list_transform_batches(node_code) if node_code else []
        versions = store.list_publish_snapshots(entry_id)
        return {
            "发布版本": entry.get("版本编号", ""),
            "数据集名称": entry.get("数据集名称", ""),
            "来源转换节点": node_code,
            "定位版本": (target or {}).get("version", ""),
            "调用链": {
                "节点编号": node_code,
                "输入表名": (node or {}).get("输入表名", ""),
                "输出表名": (node or {}).get("输出表名", ""),
                "转换批次": [batch.get("batch_no") for batch in batches],
            },
            "批次": [
                {
                    "batch_no": batch.get("batch_no"),
                    "status": batch.get("status"),
                    "row_count": batch.get("row_count", 0),
                    "integrity": batch.get("integrity", False),
                    "completed_at": batch.get("completed_at", ""),
                }
                for batch in batches
            ],
            "发布快照版本": [self._public_snapshot(snapshot) for snapshot in versions],
            "完整版本": [
                {
                    "version": snapshot.get("version"),
                    "batch_no": snapshot.get("batch_no"),
                    "row_count": snapshot.get("row_count", 0),
                    # Bugfix 交接只承认行数与冻结内容一致的完整版本。
                    "complete": len(snapshot.get("rows", [])) == int(
                        snapshot.get("row_count", len(snapshot.get("rows", [])))),
                }
                for snapshot in versions
            ],
        }
