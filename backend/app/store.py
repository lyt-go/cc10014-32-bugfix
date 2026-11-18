"""内存数据仓库：支持模块间关联、版本管理、模拟表数据、血缘记录与状态流转。

真实项目里这里会换成数据库访问层；当前实现只依赖标准库，保证克隆下来就能起。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from app.seed import SEED_ROWS


class Store:
    def __init__(self) -> None:
        self._tables: dict[str, list[dict[str, Any]]] = {
            name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()
        }
        # 模拟真实表数据（分层存储的实际行数据）
        self._table_data: dict[str, list[dict[str, Any]]] = {}
        # 版本历史: "module:id" -> [{version, timestamp, data}, ...]
        self._versions: dict[str, list[dict[str, Any]]] = {}
        # 血缘边记录
        self._lineage_edges: list[dict[str, Any]] = []
        # 质量稽核结果
        self._quality_results: dict[str, list[dict[str, Any]]] = {}
        # 快照冻结数据: "snapshot_id" -> {table_name, rows, frozen_at}
        self._snapshot_data: dict[str, dict[str, Any]] = {}
        # 发布快照版本: "发布版本id:版本号" -> 冻结批次
        self._publish_snapshots: dict[str, dict[str, Any]] = {}
        # 转换批次: 批次号 -> 批次记录
        self._transform_batches: dict[str, dict[str, Any]] = {}
        self._transform_batch_seq = 0
        # 下载书签: token -> 发布快照版本下载指针
        self._download_bookmarks: dict[str, dict[str, Any]] = {}
        # 归档记录
        self._archive_records: list[dict[str, Any]] = []
        # 补数区间记录
        self._backfill_partitions: dict[str, list[dict[str, Any]]] = {}

    def module_names(self) -> list[str]:
        return sorted(self._tables)

    def reset(self) -> None:
        """恢复示例数据并清空所有运行期状态（批次、版本、冻结数据、书签）。"""
        self._tables = {name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()}
        self._table_data.clear()
        self._versions.clear()
        self._lineage_edges.clear()
        self._quality_results.clear()
        self._snapshot_data.clear()
        self._publish_snapshots.clear()
        self._transform_batches.clear()
        self._transform_batch_seq = 0
        self._download_bookmarks.clear()
        self._archive_records.clear()
        self._backfill_partitions.clear()

    def rows(self, module: str) -> list[dict[str, Any]]:
        return self._tables.setdefault(module, [])

    def find(self, module: str, entry_id: int) -> dict[str, Any] | None:
        for row in self.rows(module):
            if int(row.get("id", 0)) == entry_id:
                return row
        return None

    def find_by_field(self, module: str, field: str, value: Any) -> dict[str, Any] | None:
        for row in self.rows(module):
            if row.get(field) == value:
                return row
        return None

    def find_all_by_field(self, module: str, field: str, value: Any) -> list[dict[str, Any]]:
        return [row for row in self.rows(module) if row.get(field) == value]

    def overview(self) -> dict[str, object]:
        modules: list[dict[str, object]] = []
        for name in self.module_names():
            rows = self.rows(name)
            modules.append({
                "name": name,
                "created": len(rows),
                "pending": sum(1 for row in rows if row.get("pending")),
                "abnormal": sum(1 for row in rows if row.get("abnormal")),
            })
        cards = [
            {"label": "业务模块", "value": len(modules)},
            {"label": "今日新增", "value": sum(int(item["created"]) for item in modules)},
            {"label": "待处理", "value": sum(int(item["pending"]) for item in modules)},
            {"label": "异常量", "value": sum(int(item["abnormal"]) for item in modules)},
        ]
        return {"cards": cards, "modules": modules}

    # ---- 模拟真实表数据 ----

    def table_rows(self, table_name: str) -> list[dict[str, Any]]:
        return self._table_data.setdefault(table_name, [])

    def set_table_rows(self, table_name: str, rows: list[dict[str, Any]]) -> None:
        self._table_data[table_name] = rows

    def append_table_rows(self, table_name: str, rows: list[dict[str, Any]]) -> None:
        self._table_data.setdefault(table_name, []).extend(rows)

    # ---- 版本管理 ----

    def _version_key(self, module: str, entry_id: int) -> str:
        return f"{module}:{entry_id}"

    def save_version(self, module: str, entry_id: int, data: dict[str, Any]) -> dict[str, Any]:
        key = self._version_key(module, entry_id)
        versions = self._versions.setdefault(key, [])
        version = {
            "version": len(versions) + 1,
            "timestamp": datetime.now().isoformat(),
            "data": dict(data),
        }
        versions.append(version)
        return version

    def get_versions(self, module: str, entry_id: int) -> list[dict[str, Any]]:
        key = self._version_key(module, entry_id)
        return list(self._versions.get(key, []))

    def get_latest_version(self, module: str, entry_id: int) -> dict[str, Any] | None:
        versions = self.get_versions(module, entry_id)
        return versions[-1] if versions else None

    # ---- 血缘 ----

    def add_lineage(self, upstream: str, downstream: str, field_map: str,
                    transform_logic: str = "", batch_id: str = "") -> dict[str, Any]:
        edge = {
            "id": len(self._lineage_edges) + 1,
            "上游表名": upstream,
            "下游表名": downstream,
            "来源字段": field_map,
            "目标字段": field_map,
            "转换逻辑": transform_logic,
            "采集批次": batch_id,
            "链路版本": f"v{len(self._lineage_edges) + 1}",
            "status": "已生效",
            "pending": False,
            "abnormal": False,
        }
        self._lineage_edges.append(edge)
        # 同时写入 lineage_track 模块表供查询
        self.rows("lineage_track").append(edge)
        return edge

    def get_lineage_edges(self) -> list[dict[str, Any]]:
        return list(self._lineage_edges)

    # ---- 质量稽核结果 ----

    def save_quality_result(self, rule_id: str, result: dict[str, Any]) -> None:
        self._quality_results.setdefault(rule_id, []).append(result)

    def get_quality_results(self, rule_id: str) -> list[dict[str, Any]]:
        return list(self._quality_results.get(rule_id, []))

    # ---- 快照 ----

    def save_snapshot_data(self, snapshot_id: str, table_name: str, rows: list[dict[str, Any]]) -> None:
        # 冻结时整批复制并保留原表名，供发布/归档按自己的批次读取，不回落实时表。
        self._snapshot_data[snapshot_id] = {
            "table_name": table_name,
            "rows": [dict(r) for r in rows],
            "frozen_at": datetime.now().isoformat(),
            "row_count": len(rows),
        }

    def get_snapshot_data(self, snapshot_id: str) -> dict[str, Any] | None:
        return self._snapshot_data.get(snapshot_id)

    # ---- 发布快照版本 ----
    #
    # 发布快照版本是发布版本在发布那一刻的完整批次副本：行与查询字段一起冻结，
    # 之后转换节点再执行只会产生新的批次，不会改写已冻结的发布快照。

    def _publish_key(self, publish_entry_id: int, version_no: int) -> str:
        return f"{publish_entry_id}:{version_no}"

    def save_publish_snapshot(self, publish_entry_id: int, version_no: int, snapshot: dict[str, Any]) -> None:
        """写入一个发布快照版本。版本归属已被占用时抛 ValueError，由调用方决定报错还是复用。"""
        key = self._publish_key(publish_entry_id, version_no)
        if key in self._publish_snapshots:
            raise ValueError(f"发布版本 {publish_entry_id} 的版本 {version_no} 已归属其他发布快照")
        record = dict(snapshot)
        record["version_no"] = version_no
        record["publish_entry_id"] = publish_entry_id
        self._publish_snapshots[key] = record

    def update_publish_snapshot(self, publish_entry_id: int, version_no: int,
                                patch: dict[str, Any]) -> dict[str, Any] | None:
        record = self._publish_snapshots.get(self._publish_key(publish_entry_id, version_no))
        if record is None:
            return None
        record.update(patch)
        return record

    def get_publish_snapshot(self, publish_entry_id: int, version_no: int) -> dict[str, Any] | None:
        return self._publish_snapshots.get(self._publish_key(publish_entry_id, version_no))

    def list_publish_snapshots(self, publish_entry_id: int) -> list[dict[str, Any]]:
        items = [
            record for key, record in self._publish_snapshots.items()
            if key.startswith(f"{publish_entry_id}:")
        ]
        return sorted(items, key=lambda item: int(item.get("version_no", 0)))

    def list_all_publish_snapshots(self) -> list[dict[str, Any]]:
        return sorted(
            self._publish_snapshots.values(),
            key=lambda item: (int(item.get("publish_entry_id", 0)), int(item.get("version_no", 0))),
        )

    def max_publish_version(self, publish_entry_id: int) -> int:
        versions = [int(item.get("version_no", 0)) for item in self.list_publish_snapshots(publish_entry_id)]
        return max(versions, default=0)

    # ---- 转换批次 ----

    def next_transform_batch(self, node_code: str | None = None) -> int:
        """分配转换批次号；批次号单调递增，重复执行不会复用旧批次。"""
        self._transform_batch_seq += 1
        return self._transform_batch_seq

    def save_transform_batch(self, batch: dict[str, Any]) -> None:
        # 同一批次号从「执行中」推进到「已完成/失败」时按批次号覆盖，避免留下半成品记录。
        self._transform_batches[str(batch.get("batch_no"))] = dict(batch)

    def get_transform_batch(self, batch_no: int | str) -> dict[str, Any] | None:
        return self._transform_batches.get(str(batch_no))

    def list_transform_batches(self, node_code: str | None = None) -> list[dict[str, Any]]:
        items = list(self._transform_batches.values())
        if node_code:
            items = [item for item in items if item.get("node_code") == node_code]
        return sorted(items, key=lambda item: int(item.get("batch_no", 0)))

    def latest_transform_batch(self, node_code: str) -> dict[str, Any] | None:
        batches = self.list_transform_batches(node_code)
        return batches[-1] if batches else None

    # ---- 下载书签 ----

    def save_download_bookmark(self, token: str, bookmark: dict[str, Any]) -> None:
        self._download_bookmarks[token] = dict(bookmark)

    def get_download_bookmark(self, token: str) -> dict[str, Any] | None:
        return self._download_bookmarks.get(token)

    def list_download_bookmarks(self, publish_entry_id: int | None = None) -> list[dict[str, Any]]:
        items = list(self._download_bookmarks.values())
        if publish_entry_id is not None:
            items = [item for item in items if int(item.get("publish_entry_id", 0)) == publish_entry_id]
        return sorted(items, key=lambda item: item.get("created_at", ""))

    # ---- 归档 ----

    def add_archive_record(self, record: dict[str, Any]) -> None:
        self._archive_records.append(record)

    def get_archive_records(self) -> list[dict[str, Any]]:
        return list(self._archive_records)

    # ---- 补数分区 ----

    def save_backfill_partition(self, job_id: str, partition: dict[str, Any]) -> None:
        self._backfill_partitions.setdefault(job_id, []).append(partition)

    def get_backfill_partitions(self, job_id: str) -> list[dict[str, Any]]:
        return list(self._backfill_partitions.get(job_id, []))


store = Store()
