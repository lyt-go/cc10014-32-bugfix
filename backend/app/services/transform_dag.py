"""转换编排 DAG 图服务：支持节点执行时消费上游表数据，写入下游表，并自动生成血缘。"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from app.store import store

MODULE = "transform_dag"
REQUIRED_FIELDS = ["节点编号", "输入表名", "输出表名"]
STATUS_ORDER = ["草稿", "待校验", "已生效", "已停用"]
ACTION_RULES = {"提交校验": "待校验", "标记生效": "已生效", "停用节点": "已停用"}
NEGATIVE_ACTIONS = ["停用节点"]


def csv_cell(value: Any) -> str:
    text = str(value) if value is not None else ""
    if text and text[0] in "=+-@":
        text = "'" + text
    return '"' + text.replace('"', '""') + '"'


class TransformDagService:
    def get_graph(self) -> dict[str, Any]:
        rows = store.rows(MODULE)
        nodes: list[dict] = []
        edges: list[dict] = []
        for i, row in enumerate(rows):
            nodes.append({"id": int(row.get("id", i + 1)),
                          "name": str(row.get("节点编号", "")),
                          "status": str(row.get("status", STATUS_ORDER[0])),
                          **{k: row[k] for k in row if k not in ("id", "status", "pending", "abnormal")}})
        # 基于依赖节点构建真实边
        name_to_id = {n["name"]: n["id"] for n in nodes}
        for node in nodes:
            dep = str(node.get("依赖节点", ""))
            if dep and dep in name_to_id:
                edges.append({"from": name_to_id[dep], "to": node["id"], "label": "依赖"})
        # 补充基于输入输出表的血缘边
        for i, a in enumerate(nodes):
            for b in nodes[i + 1:]:
                if a.get("输出表名") and a["输出表名"] == b.get("输入表名"):
                    edges.append({"from": a["id"], "to": b["id"], "label": "表依赖"})
        return {"nodes": nodes, "edges": edges}

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [f for f in REQUIRED_FIELDS if not str(values.get(f) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(r.get("id", 0)) for r in rows), default=0) + 1}
        entry.update({f: values.get(f) for f in REQUIRED_FIELDS})
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        for k in ["转换脚本", "执行顺序", "依赖节点", "失败策略", "调度周期"]:
            if k in values:
                entry[k] = values[k]
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"转换节点 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于转换编排可执行范围"
        target = ACTION_RULES[action]
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        store.save_version(MODULE, entry_id, entry)
        return entry, f"转换节点已{action}"

    def raw_data(self) -> tuple[list[dict], list[dict]]:
        g = self.get_graph()
        return g["nodes"], g["edges"]

    # ------------------------------------------------------------------
    # 跨模块持久化的发布快照版本：转换编排详情页与数据发布看板共用同一份口径
    # ------------------------------------------------------------------
    def get_publish_versions(self, entry_id: int) -> dict[str, Any]:
        """沿节点编号定位发布版本，返回其发布快照版本（由数据发布模块持久化）。"""
        entry = self.get_entry(entry_id)
        if entry is None:
            return {"ok": False, "message": f"转换节点 {entry_id} 不存在", "items": []}
        node_code = entry.get("节点编号", "")
        output_table = entry.get("输出表名", "")
        batches = store.list_transform_batches(node_code)
        publish_entries = [
            row for row in store.rows("data_publish")
            if row.get("来源转换节点") == node_code
        ]
        if not publish_entries and output_table:
            # 老数据只有数据集名称，按输出表名兜底定位，避免详情页读不到发布版本。
            publish_entries = [
                row for row in store.rows("data_publish")
                if row.get("数据集名称") == output_table
            ]
        from app.services.data_publish import DataPublishService
        publisher = DataPublishService()
        versions = []
        for publish_entry in publish_entries:
            for snapshot in publisher.list_versions(int(publish_entry["id"])):
                versions.append({
                    **snapshot,
                    "发布版本id": int(publish_entry["id"]),
                    "发布版本编号": publish_entry.get("版本编号", ""),
                    "发布状态": publish_entry.get("status", ""),
                })
        # 只保留与当前节点批次相关的版本，避免历史别的节点的版本串到详情页。
        node_batches = {str(batch.get("batch_no")) for batch in batches}
        related = [item for item in versions if str(item.get("batch_no")) in node_batches or item.get("batch_no") == "MANUAL"]
        return {
            "ok": True,
            "node_code": node_code,
            "output_table": output_table,
            "batches": [
                {
                    "batch_no": batch.get("batch_no"),
                    "status": batch.get("status"),
                    "integrity": batch.get("integrity", False),
                    "row_count": batch.get("row_count", 0),
                }
                for batch in batches
            ],
            "publish_entries": [
                {"id": int(row["id"]), "版本编号": row.get("版本编号", ""),
                 "数据集名称": row.get("数据集名称", ""), "status": row.get("status", "")}
                for row in publish_entries
            ],
            "items": related,
            "total": len(related),
        }

    def execute_node(self, entry_id: int) -> dict[str, Any]:
        """执行转换节点：读取输入表数据，按转换脚本逻辑处理，写入输出表，并自动生成血缘和推导schema。"""
        entry = self.get_entry(entry_id)
        if not entry:
            return {"ok": False, "message": "转换节点不存在"}
        input_table = entry.get("输入表名", "")
        output_table = entry.get("输出表名", "")
        script = entry.get("转换脚本", "")
        # 读取输入数据
        input_data = store.table_rows(input_table)
        if not input_data:
            from app.services.dwd_layer import DwdLayerService
            dwd = DwdLayerService()
            # 尝试构建DWD数据
            dwd_entry = store.find_by_field("dwd_layer", "表名称", input_table)
            if dwd_entry:
                dwd.build_dwd_from_ods(dwd_entry["id"])
            input_data = store.table_rows(input_table)
        if not input_data:
            return {"ok": False, "message": f"输入表 {input_table} 无数据"}
        # 每次执行分配独立批次，批次是发布快照唯一的取数口径。
        batch_no = store.next_transform_batch(entry.get("节点编号", ""))
        batch = {
            "batch_no": batch_no,
            "node_code": entry.get("节点编号", ""),
            "input_table": input_table,
            "output_table": output_table,
            "status": "执行中",
            "integrity": False,
            "row_count": 0,
            "rows": [],
            "started_at": datetime.now().isoformat(),
            "completed_at": "",
        }
        store.save_transform_batch(batch)
        try:
            # 简单模拟SQL解析：GROUP BY 聚合
            output_data = self._execute_script(input_data, script)
            if not isinstance(output_data, list):
                raise ValueError("转换输出不是完整行集")
            # 先落盘输出表，再把同一批完整行集写入批次；两者行数必须一致。
            store.set_table_rows(output_table, output_data)
            store.save_transform_batch({**batch, "status": "已完成", "integrity": True,
                                        "row_count": len(output_data), "rows": output_data,
                                        "completed_at": datetime.now().isoformat()})
        except Exception as exc:  # 事务回滚：半成品批次不允许被发布消费
            store.save_transform_batch({**batch, "status": "失败", "integrity": False,
                                        "row_count": 0, "rows": [], "error": str(exc),
                                        "completed_at": datetime.now().isoformat()})
            return {"ok": False, "message": f"转换批次 {batch_no} 执行失败：{exc}"}
        # 自动生成血缘
        store.add_lineage(input_table, output_table,
                          field_map=entry.get("节点编号", ""),
                          transform_logic=script,
                          batch_id=f"{entry.get('节点编号', '')}#{batch_no}")
        # 自动推导schema变更并注册
        from app.services.schema_registry import SchemaRegistryService
        sch = SchemaRegistryService()
        sch.derive_schema_from_lineage(output_table)
        return {"ok": True, "message": f"节点执行完成，输出{len(output_data)}行到 {output_table}",
                "rows": output_data, "batch_no": batch_no, "row_count": len(output_data)}

    def _execute_script(self, data: list[dict[str, Any]], script: str) -> list[dict[str, Any]]:
        if "GROUP BY" in script.upper():
            import re
            # 找聚合字段：SUM/COUNT/AVG/MAX/MIN(...)
            agg_m = re.search(r"(SUM|COUNT|AVG|MAX|MIN)\s*\(([^)]+)\)", script, re.I)
            measure = agg_m.group(2).strip() if agg_m else ""
            agg = (agg_m.group(1).upper() if agg_m else "SUM")
            # 提取 GROUP BY 后面的维度字段（排除聚合函数部分），保留原始大小写
            after_gb = script.split("GROUP BY", 1)[1].strip() if "GROUP BY" in script.upper() else script
            # 去掉末尾的聚合函数部分
            agg_keywords = [" SUM(", " COUNT(", " AVG(", " MAX(", " MIN(",
                          " sum(", " count(", " avg(", " max(", " min("]
            for kw in agg_keywords:
                if kw in after_gb.upper():
                    idx = after_gb.upper().index(kw)
                    after_gb = after_gb[:idx]
                    break
            dims_raw = [d.strip() for d in after_gb.split(",")]
            dims = [d for d in dims_raw if d and not re.search(r'^(SUM|COUNT|AVG|MAX|MIN)\s*\(', d, re.I)]
            groups: dict[str, dict[str, Any]] = {}
            for row in data:
                key = "|".join(str(row.get(d, "")) for d in dims) if dims else "*"
                if key not in groups:
                    groups[key] = {d: row.get(d) for d in dims} if dims else {}
                    groups[key]["_values"] = []
                try:
                    groups[key]["_values"].append(float(row.get(measure, 0) or 0))
                except Exception:
                    groups[key]["_values"].append(0.0)
            result = []
            for g in groups.values():
                out = {d: g[d] for d in dims} if dims else {}
                vals = g["_values"]
                if agg == "SUM":
                    out[f"{measure}_agg"] = sum(vals)
                elif agg == "COUNT":
                    out[f"{measure}_agg"] = len(vals)
                elif agg == "AVG":
                    out[f"{measure}_agg"] = sum(vals) / len(vals) if vals else 0
                elif agg == "MAX":
                    out[f"{measure}_agg"] = max(vals) if vals else 0
                elif agg == "MIN":
                    out[f"{measure}_agg"] = min(vals) if vals else 0
                else:
                    out[f"{measure}_agg"] = sum(vals)
                result.append(out)
            return result
        # 默认透传
        return [dict(r) for r in data]
