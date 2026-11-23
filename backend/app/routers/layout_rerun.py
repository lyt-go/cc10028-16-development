"""版式复跑包接口：维护页修订、导出可离线复跑的包、下载与复跑结果核对。

路径挂在固定前缀下，并由 ``app.main`` 在通用 `/{module}` 路由之前注册，
避免被动态模块路由抢先匹配。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from app.services.layout_revision import (
    LayoutRevisionError,
    LayoutRevisionNotFound,
    LayoutRevisionRepo,
    LayoutRerunPackage,
)

router = APIRouter(prefix="/api/layout-rerun", tags=["版式复跑包"])
repo = LayoutRevisionRepo()
packages = LayoutRerunPackage(repo)


class PageRegionPayload(BaseModel):
    kind: str = "table"
    bbox: list[float] | str
    rows: int | None = None
    cols: int | None = None
    header_rows: int | None = None
    merged_cells: int | None = None
    reading_order: int | None = None
    confidence: float | None = None
    language: str | None = None
    cells: list[list[Any]] | None = None


class PagePayload(BaseModel):
    page: int = Field(ge=1)
    page_status: str = "待确认"
    confirmed_at: str | None = None
    regions: list[PageRegionPayload] = Field(default_factory=list)
    source: dict[str, Any] = Field(default_factory=dict)


class DocumentPayload(BaseModel):
    total_pages: int = Field(ge=1)
    pages: list[PagePayload]


class PageUpdatePayload(BaseModel):
    page_status: str | None = None
    confirmed_at: str | None = None
    regions: list[PageRegionPayload] | None = None
    source: dict[str, Any] | None = None
    expected_revision: int | None = Field(default=None, ge=1)


class PackageRequest(BaseModel):
    doc_id: str
    pages: list[int] | None = None
    exported_by: str | None = None


def _fail(exc: Exception) -> HTTPException:
    status = 404 if isinstance(exc, LayoutRevisionNotFound) else 400
    return HTTPException(status_code=status, detail=str(exc))


@router.post("/documents/{doc_id}/pages")
def save_document(doc_id: str, payload: DocumentPayload) -> dict[str, Any]:
    try:
        saved = repo.save_document(doc_id, payload.total_pages,
                                   [page.model_dump(exclude_none=True) for page in payload.pages])
    except LayoutRevisionError as exc:
        raise _fail(exc)
    return {"ok": True, "doc_id": doc_id, "pages": [saved[number]["page"] for number in sorted(saved)]}


@router.post("/documents/{doc_id}/seed")
def seed_document(doc_id: str) -> dict[str, Any]:
    """从解析/版式还原产物建立待确认页修订，供后续人工更正与确认。"""
    try:
        saved = repo.seed_from_artifacts(doc_id)
    except LayoutRevisionError as exc:
        raise _fail(exc)
    return {
        "ok": True,
        "doc_id": doc_id,
        "pages": [{
            "page": page["page"],
            "page_revision": page["page_revision"],
            "page_status": page["page_status"],
            "table_count": sum(1 for region in page["regions"] if region["kind"] == "table"),
        } for _, page in sorted(saved.items())],
    }


@router.get("/documents")
def list_documents() -> dict[str, Any]:
    return {"ok": True, "documents": repo.list_documents()}


@router.get("/documents/{doc_id}/pages")
def list_pages(doc_id: str) -> dict[str, Any]:
    try:
        pages = repo.list_pages(doc_id)
    except LayoutRevisionError as exc:
        raise _fail(exc)
    return {
        "ok": True,
        "doc_id": doc_id,
        "total_pages": repo.total_pages(doc_id),
        "pages": [{
            "page": page["page"],
            "page_revision": page["page_revision"],
            "page_status": page["page_status"],
            "confirmed_at": page.get("confirmed_at"),
            "region_count": len(page["regions"]),
            "table_count": sum(1 for region in page["regions"] if region["kind"] == "table"),
        } for page in pages],
    }


@router.put("/documents/{doc_id}/pages/{page}")
def update_page(doc_id: str, page: int, payload: PageUpdatePayload) -> dict[str, Any]:
    values = payload.model_dump(exclude_none=True)
    regions = values.pop("regions", None)
    try:
        revision = repo.update_page(doc_id, page, regions=regions, **values)
    except LayoutRevisionError as exc:
        raise _fail(exc)
    return {
        "ok": True,
        "doc_id": doc_id,
        "page": revision["page"],
        "page_revision": revision["page_revision"],
        "page_status": revision["page_status"],
        "confirmed_at": revision.get("confirmed_at"),
        "region_checksum": revision["region_checksum"],
        "page_checksum": revision["page_checksum"],
    }


@router.get("/documents/{doc_id}/pages/{page}/revisions")
def page_revisions(doc_id: str, page: int) -> dict[str, Any]:
    try:
        items = repo.revisions(doc_id, page)
    except LayoutRevisionError as exc:
        raise _fail(exc)
    return {
        "ok": True,
        "doc_id": doc_id,
        "page": page,
        "revisions": [{
            "page_revision": item["page_revision"],
            "page_status": item["page_status"],
            "confirmed_at": item.get("confirmed_at"),
            "page_checksum": item["page_checksum"],
            "region_checksum": item["region_checksum"],
            "created_at": item.get("created_at"),
        } for item in items],
    }


@router.post("/packages")
def create_package(payload: PackageRequest) -> JSONResponse:
    try:
        path, built = packages.write_zip(payload.doc_id, payload.pages, exported_by=payload.exported_by)
    except LayoutRevisionError as exc:
        raise _fail(exc)
    manifest = built["manifest"]
    return JSONResponse({
        "ok": True,
        "package_id": manifest["package_id"],
        "doc_id": manifest["doc_id"],
        "selected_pages": manifest["selected_pages"],
        "readiness": manifest["readiness"],
        "batch_table_count": manifest["batch_table_count"],
        "download_url": f"/api/layout-rerun/packages/{manifest['package_id']}/download",
        "file": path.name,
    })


@router.get("/packages")
def list_packages() -> dict[str, Any]:
    return {"ok": True, "packages": packages.list_packages()}


@router.get("/packages/{package_id}/download")
def download_package(package_id: str) -> FileResponse:
    path = packages.package_path(package_id)
    if path is None:
        raise HTTPException(status_code=404, detail=f"复跑包 {package_id} 不存在")
    return FileResponse(path, media_type="application/zip", filename=path.name)


@router.get("/packages/{package_id}/verify")
def verify_package(package_id: str) -> dict[str, Any]:
    path = packages.package_path(package_id)
    if path is None:
        raise HTTPException(status_code=404, detail=f"复跑包 {package_id} 不存在")
    try:
        loaded = packages.load_for_extraction(path)
    except LayoutRevisionError as exc:
        raise _fail(exc)
    return {"ok": True, "manifest": loaded["manifest"], "pages": loaded["pages"]}


@router.post("/packages/{package_id}/table-extract")
def extract_tables(package_id: str,
                   pages: str | None = Query(default=None),
                   out_dir: str | None = Query(default=None),
                   include_unconfirmed: bool = Query(default=False)) -> dict[str, Any]:
    from app.services.table_extract import TableExtractService

    path = packages.package_path(package_id)
    if path is None:
        raise HTTPException(status_code=404, detail=f"复跑包 {package_id} 不存在")
    page_numbers = [int(item) for item in pages.split(",") if item.strip()] if pages else None
    try:
        service = TableExtractService()
        result = service.extract_from_package(
            path, page_numbers=page_numbers, out_dir=out_dir,
            include_unconfirmed=include_unconfirmed)
    except LayoutRevisionError as exc:
        raise _fail(exc)
    return {"ok": True, **result}
