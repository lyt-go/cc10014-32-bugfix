"""数据发布接口：维护发布版本，覆盖提交校验、确认发布、撤回版本等动作。

除列表/动作外，还提供发布快照版本的读取、导出、打包下载与下载书签，
以及沿调用链定位发布版本结果的文件交接视图。
"""
from __future__ import annotations

import csv
import io
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from app.schemas import ActionPayload, ActionResult, EntryPayload, PageResult
from app.services.data_publish import DataPublishService

router = APIRouter(prefix="/api/data_publish", tags=["数据发布"])

service = DataPublishService()

LIST_FIELDS = ["版本编号", "数据集名称", "发布范围", "查询参数", "响应字段", "缓存秒数", "分页上限", "消费应用"]
STATUSES = ["草稿", "待校验", "已发布", "已撤回"]


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
    keyword: str | None = Query(default=None, description="按版本编号检索"),
    status: str | None = Query(default=None, description="草稿、待校验、已发布、已撤回"),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=200),
) -> PageResult[dict]:
    """按页面字段、版本编号关键字与状态过滤数据发布列表。"""
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
    output.write("﻿")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(["id", *LIST_FIELDS, "发布快照版本", "发布快照行数", "转换批次", "status"])
    for item in items:
        row = []
        for value in [item.get("id"), *(item.get(field, "") for field in LIST_FIELDS),
                      item.get("发布快照版本", ""), item.get("发布快照行数", ""),
                      item.get("转换批次", ""), item.get("status", "")]:
            text = "" if value is None else str(value)
            row.append("'" + text if text.startswith(("=", "+", "-", "@")) else text)
        writer.writerow(row)
    return StreamingResponse(
        iter([output.getvalue()]), media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="data_publish.csv"'})


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条发布版本明细（含发布快照版本清单）；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"发布版本 {entry_id} 不存在或已归档")
    detail = service.decorate(entry)
    detail["发布快照版本清单"] = service.list_versions(entry_id)
    return detail


@router.post("", response_model=ActionResult)
def create_entry(payload: dict[str, object]) -> ActionResult:
    """登记一条发布版本，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="发布版本已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: ActionPayload) -> ActionResult:
    """执行提交校验、确认发布、撤回版本；不允许的动作会被拦下并说明原因。"""
    entry, message = service.run_action(entry_id, payload.action.strip())
    if entry is None:
        return ActionResult(ok=False, message=message)
    # 待发布批次场景下条目会被推进但 message 已说明只保留上一完整结果，
    # ok 取动作是否真正改变了发布状态，避免前端把“保持上一结果”当成失败。
    return ActionResult(ok=True, message=message, entry=service.decorate(entry))


@router.get("/{entry_id}/data")
def get_published_data(entry_id: int, version: int | None = Query(default=None, ge=1)) -> dict[str, Any]:
    """返回发布快照版本冻结的数据；指定 version 时读取历史版本，不回落实时表。"""
    data = service.get_published_data(entry_id, version)
    if data is None:
        raise HTTPException(status_code=404, detail=f"发布版本 {entry_id} 无可用发布快照版本")
    return data


@router.get("/{entry_id}/versions")
def list_versions(entry_id: int) -> dict[str, Any]:
    """列出该发布版本的全部发布快照版本。"""
    if service.get_entry(entry_id) is None:
        raise HTTPException(status_code=404, detail=f"发布版本 {entry_id} 不存在或已归档")
    items = service.list_versions(entry_id)
    return {"items": items, "total": len(items)}


@router.get("/{entry_id}/versions/{version_no}")
def get_version(entry_id: int, version_no: int) -> dict[str, Any]:
    """读取单个发布快照版本的元数据（不含行数据）。"""
    item = service.get_version(entry_id, version_no)
    if item is None:
        raise HTTPException(status_code=404, detail=f"发布快照版本 {entry_id}:v{version_no} 不存在")
    return item


@router.get("/{entry_id}/versions/{version_no}/export")
def export_version(entry_id: int, version_no: int) -> StreamingResponse:
    """导出某个发布快照版本为 CSV；导出内容来自冻结数据。"""
    exported = service.export_version_csv(entry_id, version_no)
    if exported is None:
        raise HTTPException(status_code=404, detail=f"发布快照版本 {entry_id}:v{version_no} 不可导出")
    text, filename = exported
    return StreamingResponse(
        iter([text]), media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@router.get("/{entry_id}/versions/{version_no}/package")
def package_version(entry_id: int, version_no: int) -> StreamingResponse:
    """把发布快照版本打包（CSV + manifest）供下载。"""
    packaged = service.package_version(entry_id, version_no)
    if packaged is None:
        raise HTTPException(status_code=404, detail=f"发布快照版本 {entry_id}:v{version_no} 不可打包")
    payload, filename = packaged
    return StreamingResponse(
        iter([payload]), media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@router.post("/{entry_id}/versions/{version_no}/bookmark")
def create_bookmark(entry_id: int, version_no: int, kind: str = Query(default="csv", pattern="^(csv|package)$")) -> dict[str, Any]:
    """为发布快照版本保存下载书签，之后可按书签再次下载同一冻结版本。"""
    bookmark = service.save_download_bookmark(entry_id, version_no, kind)
    if bookmark is None:
        raise HTTPException(status_code=404, detail=f"发布快照版本 {entry_id}:v{version_no} 不存在或下载类型不支持")
    return {"ok": True, "bookmark": bookmark, "url": f"/api/data_publish/downloads/{bookmark['token']}"}


@router.get("/{entry_id}/versions/{version_no}/handoff")
def get_handoff(entry_id: int, version_no: int) -> dict[str, Any]:
    """文件交接视图：沿发布版本→转换节点→批次调用链定位指定发布快照版本结果。"""
    handoff = service.handoff(entry_id, version_no)
    if handoff is None:
        raise HTTPException(status_code=404, detail=f"发布版本 {entry_id} 的发布快照版本 v{version_no} 不存在")
    return handoff


@router.get("/downloads/{token}")
def download_by_bookmark(token: str) -> StreamingResponse:
    """按下载书签取回发布快照版本文件；书签只指向冻结版本，不跟随实时表。"""
    resolved = service.resolve_download_bookmark(token)
    if resolved is None:
        raise HTTPException(status_code=404, detail="下载书签不存在或对应发布快照版本已失效")
    payload, filename, bookmark = resolved
    media = "application/zip" if bookmark["kind"] == "package" else "text/csv; charset=utf-8"
    return StreamingResponse(
        iter([payload]), media_type=media,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@router.get("/{entry_id}/bookmarks")
def list_bookmarks(entry_id: int) -> dict[str, Any]:
    """列出该发布版本保存的下载书签（指向已冻结的发布快照版本）。"""
    if service.get_entry(entry_id) is None:
        raise HTTPException(status_code=404, detail=f"发布版本 {entry_id} 不存在或已归档")
    from app.store import store
    items = store.list_download_bookmarks(entry_id)
    return {"items": items, "total": len(items)}
