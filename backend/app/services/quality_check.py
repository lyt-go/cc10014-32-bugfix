"""质量稽核业务规则：支持读取目标表数据并执行校验表达式，记录稽核结果。"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from app.store import store

MODULE = "quality_check"
LIST_FIELDS = ["规则编号", "校验表名", "校验字段", "校验表达式", "阈值下界", "阈值上界", "阻断级别", "采样比例"]
REQUIRED_FIELDS = ["规则编号", "校验表名", "校验字段"]
STATUS_ORDER = ["草稿", "待校验", "已生效", "已停用"]
ACTION_RULES = {"提交校验": "待校验", "标记生效": "已生效", "停用规则": "已停用"}
NEGATIVE_ACTIONS = ["停用规则"]


class QualityCheckService:
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
            rows = [row for row in rows if keyword.casefold() in str(row.get("规则编号", "")).casefold()]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = (page - 1) * size
        return rows[start:start + size], total

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
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"稽核规则 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于质量稽核可执行范围"
        target = ACTION_RULES[action]
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        store.save_version(MODULE, entry_id, entry)
        return entry, f"稽核规则已{action}"

    def execute_check(self, entry_id: int, rows: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        """读取目标表数据（或调用方给定的完整批次行），执行校验表达式，返回稽核结果。"""
        entry = self.get_entry(entry_id)
        if not entry:
            return {"ok": False, "message": "稽核规则不存在"}
        table = entry.get("校验表名", "")
        field = entry.get("校验字段", "")
        expr = entry.get("校验表达式", "")
        lower = entry.get("阈值下界", "")
        upper = entry.get("阈值上界", "")
        # 调用方给了完整批次就按批次校验（即使是空批次也照实校验），
        # 避免发布时口径漂移到实时表。
        if rows is None:
            rows = store.table_rows(table)
            if not rows:
                # 尝试从ODS读取
                from app.services.ods_layer import OdsLayerService
                ods = OdsLayerService()
                rows = ods.get_table_data(table)
        total = len(rows)
        failed = 0
        failed_rows: list[dict[str, Any]] = []
        for row in rows:
            val = row.get(field)
            ok = self._check_value(val, expr, lower, upper)
            if not ok:
                failed += 1
                failed_rows.append(row)
        result = {
            "rule_id": entry.get("规则编号"),
            "table": table,
            "field": field,
            "total": total,
            "failed": failed,
            "passed": total - failed,
            "checked_at": datetime.now().isoformat(),
            "failed_rows": failed_rows[:5],  # 只保留前5条
        }
        store.save_quality_result(entry.get("规则编号", ""), result)
        return {"ok": True, "message": f"稽核完成：通过{total - failed}/失败{failed}", "result": result}

    def _check_value(self, val: Any, expr: str, lower: str, upper: str) -> bool:
        if expr:
            # 简单表达式解析：>=0 AND <=999999 或 IN (...)
            if "AND" in expr.upper():
                parts = expr.upper().replace("AND", ",").split(",")
                for part in parts:
                    part = part.strip()
                    if ">=" in part:
                        try:
                            bound = float(part.split(">=")[1].strip())
                            if float(val or 0) < bound:
                                return False
                        except Exception:
                            pass
                    elif "<=" in part:
                        try:
                            bound = float(part.split("<=")[1].strip())
                            if float(val or 0) > bound:
                                return False
                        except Exception:
                            pass
            if "IN" in expr.upper():
                # 提取括号内值
                import re
                m = re.search(r"IN\s*\(([^)]+)\)", expr, re.I)
                if m:
                    allowed = [v.strip().strip("'") for v in m.group(1).split(",")]
                    if str(val) not in allowed:
                        return False
        if lower != "":
            try:
                if float(val or 0) < float(lower):
                    return False
            except Exception:
                pass
        if upper != "":
            try:
                if float(val or 0) > float(upper):
                    return False
            except Exception:
                pass
        return True
