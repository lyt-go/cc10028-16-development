"""版式区域版本库与版式复跑包。

版式区域按“页”保存为不可变修订：任何区域更新都会生成新的页修订，
同一页的页记录、区域集合和确认依据始终来自同一个修订，不会出现前后
版本拼出的同页。导出复跑包时先取一次快照，再把快照内容写入 ZIP，
因此导出请求与区域更新并发时，包内每一页仍然自洽。

包内附一个只依赖标准库的 ``rerun.py``，复查人员解压后可以直接离线
复跑；表格抽取也可以直接消费包内的页和区域，写出带页码、表格号、
行列坐标的单元格数据。
"""
from __future__ import annotations

import copy
import hashlib
import io
import json
import os
import re
import threading
import uuid
import zipfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

try:  # Linux 环境用文件锁保证多进程写修订与导出快照互斥
    import fcntl
except ImportError:  # pragma: no cover - 非 POSIX 平台退化为进程内锁
    fcntl = None

from app.services.base import DATA_ROOT
from app.store import store

PAGE_SCHEMA = "layout-rerun/page/v1"
MANIFEST_SCHEMA = "layout-rerun/manifest/v1"
INDEX_SCHEMA = "layout-rerun/index/v1"
EVIDENCE_SCHEMA = "layout-rerun/evidence/v1"
PACKAGE_FORMAT_VERSION = "1.0"

REVISIONS_ROOT = DATA_ROOT / "layout_revisions"
PACKAGES_ROOT = DATA_ROOT / "rerun_packages"
PACKAGE_INDEX = PACKAGES_ROOT / "index.json"
RUNNER_ASSET = Path(__file__).resolve().parent.parent / "rerun_assets" / "rerun.py"

_BBOX_NUMBER = re.compile(r"-?\d+(?:\.\d+)?")
_DOC_LOCKS: dict[str, threading.RLock] = {}
_DOC_LOCKS_GUARD = threading.Lock()
_PACKAGE_LOCK = threading.RLock()


class LayoutRevisionError(ValueError):
    """版式修订 / 复跑包校验失败。"""


class LayoutRevisionNotFound(LayoutRevisionError):
    """指定的文档或页修订不存在。"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _checksum(value: Any) -> str:
    return hashlib.sha256(_dump(value).encode("utf-8")).hexdigest()


def _write_json_atomic(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _normalize_bbox(bbox: Any) -> list[float]:
    if isinstance(bbox, (list, tuple)):
        values = [float(v) for v in bbox]
    else:
        values = [float(v) for v in _BBOX_NUMBER.findall(str(bbox or ""))]
    if len(values) != 4:
        raise LayoutRevisionError(f"区域坐标必须为 x0,y0,x1,y1 四个数值，收到：{bbox!r}")
    x0, y0, x1, y1 = values
    if x1 < x0 or y1 < y0:
        raise LayoutRevisionError(f"区域坐标右下角必须不小于左上角，收到：{bbox!r}")
    return values


def _bbox_label(bbox: list[float]) -> str:
    return "(" + ", ".join(str(int(v)) if float(v).is_integer() else str(v) for v in bbox) + ")"


def _doc_lock(doc_id: str) -> threading.RLock:
    with _DOC_LOCKS_GUARD:
        return _DOC_LOCKS.setdefault(doc_id, threading.RLock())


@contextmanager
def _locked_doc(doc_id: str) -> Iterator[None]:
    """同一文档的修订与导出串行化，跨进程用 flock，跨线程用 RLock。"""
    lock = _doc_lock(doc_id)
    with lock:
        lock_file = REVISIONS_ROOT / doc_id / ".lock"
        lock_file.parent.mkdir(parents=True, exist_ok=True)
        with open(lock_file, "a+", encoding="utf-8") as handle:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                if fcntl is not None:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _region_id(page: int, region: dict[str, Any]) -> str:
    return f"p{page:03d}-{region['kind']}-{region['seq']:03d}"


def _normalize_region(page: int, region: dict[str, Any], seq: int) -> dict[str, Any]:
    kind = str(region.get("kind") or "table").strip().lower()
    if kind not in {"table", "image", "paragraph", "text"}:
        raise LayoutRevisionError(f"未知区域类型：{kind}")
    bbox = _normalize_bbox(region.get("bbox"))
    normalized: dict[str, Any] = {
        "region_id": _region_id(page, {"kind": kind, "seq": seq}),
        "kind": kind,
        "seq": seq,
        "bbox": bbox,
        "bbox_text": _bbox_label(bbox),
        "reading_order": int(region.get("reading_order", seq)),
        "confidence": float(region.get("confidence", 1.0)),
    }
    if kind == "table":
        rows = int(region.get("rows", 0) or 0)
        cols = int(region.get("cols", 0) or 0)
        if rows < 1 or cols < 1:
            raise LayoutRevisionError(f"表格区域必须给出行数和列数，收到 rows={rows}, cols={cols}")
        normalized.update({
            "rows": rows,
            "cols": cols,
            "header_rows": int(region.get("header_rows", region.get("header_row", 1)) or 1),
            "merged_cells": int(region.get("merged_cells", 0) or 0),
        })
        cells = region.get("cells")
        if cells is not None:
            normalized["cells"] = cells
    if kind == "image":
        normalized["language"] = str(region.get("language", "chinese"))
    return normalized


def _normalize_regions(page: int, regions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized = [_normalize_region(page, region, seq) for seq, region in enumerate(regions)]
    normalized.sort(key=lambda item: (item["reading_order"], item["seq"]))
    return normalized


def _page_document(doc_id: str, page: int, revision: int, status: str, confirmed_at: str | None,
                   regions: list[dict[str, Any]], source: dict[str, Any]) -> dict[str, Any]:
    body = {
        "schema": PAGE_SCHEMA,
        "doc_id": doc_id,
        "page": page,
        "page_revision": revision,
        "page_status": status,
        "confirmed_at": confirmed_at,
        "regions": regions,
        "region_checksum": _checksum(regions),
        "source": source,
        "created_at": _now(),
    }
    body["page_checksum"] = _checksum({key: value for key, value in body.items() if key != "created_at"})
    return body


class LayoutRevisionRepo:
    """按文档保存版式页修订；每一页的修订只追加，不复用文件名。"""

    def _doc_dir(self, doc_id: str) -> Path:
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", str(doc_id or "")):
            raise LayoutRevisionError(f"文档编号不合法：{doc_id!r}")
        return REVISIONS_ROOT / doc_id

    def _revision_file(self, doc_id: str, page: int, revision: int) -> Path:
        return self._doc_dir(doc_id) / f"page-{page:03d}-r{revision:03d}.json"

    def _load_payload(self, doc_id: str) -> dict[str, Any] | None:
        path = self._doc_dir(doc_id) / "pages.json"
        if not path.exists():
            return None
        return _read_json(path)

    def load(self, doc_id: str) -> dict[int, dict[str, Any]] | None:
        """读取整份文档的最新页集合；不存在返回 None。"""
        payload = self._load_payload(doc_id)
        if payload is None:
            return None
        return {int(item["page"]): item for item in payload.get("pages", [])}

    def total_pages(self, doc_id: str) -> int:
        payload = self._load_payload(doc_id) or {}
        return int(payload.get("total_pages") or 0)

    def get_page(self, doc_id: str, page: int) -> dict[str, Any] | None:
        pages = self.load(doc_id) or {}
        return pages.get(page)

    def revisions(self, doc_id: str, page: int) -> list[dict[str, Any]]:
        """按修订号列出某页的全部历史修订，供旧包追溯确认依据。"""
        doc_dir = self._doc_dir(doc_id)
        found = []
        for path in sorted(doc_dir.glob(f"page-{page:03d}-r*.json")):
            found.append(_read_json(path))
        return found

    def list_pages(self, doc_id: str) -> list[dict[str, Any]]:
        pages = self.load(doc_id)
        if pages is None:
            raise LayoutRevisionNotFound(f"文档 {doc_id} 尚无版式修订")
        return [pages[number] for number in sorted(pages)]

    def list_documents(self) -> list[dict[str, Any]]:
        """列出已有版式修订的文档及每页确认状态，供导出页选择。"""
        documents = []
        if not REVISIONS_ROOT.exists():
            return documents
        for doc_dir in sorted(REVISIONS_ROOT.iterdir()):
            if not doc_dir.is_dir():
                continue
            pages = self.load(doc_dir.name)
            if pages is None:
                continue
            documents.append({
                "doc_id": doc_dir.name,
                "total_pages": self.total_pages(doc_dir.name),
                "ready_pages": [number for number, page in sorted(pages.items())
                                if page["page_status"] == "已确认"],
                "pages": [{
                    "page": page["page"],
                    "page_revision": page["page_revision"],
                    "page_status": page["page_status"],
                    "confirmed_at": page.get("confirmed_at"),
                    "table_count": sum(1 for region in page["regions"] if region["kind"] == "table"),
                } for _, page in sorted(pages.items())],
            })
        return documents

    def seed_from_artifacts(self, doc_id: str) -> dict[int, dict[str, Any]]:
        """把解析/版式还原产物登记为待确认的页修订。

        读取 ``parsed/<doc>_parsed.jsonl``、``layout/<doc>_tables.jsonl`` 和
        ``layout/<doc>_images.jsonl``，每页生成初始修订；区域只取页码匹配的
        记录，之后的人工更正和确认都通过 update_page 生成新修订。
        """
        layout_dir = DATA_ROOT / "layout"
        parsed_records = self._read_artifacts(DATA_ROOT / "parsed" / f"{doc_id}_parsed.jsonl")
        if not parsed_records:
            raise LayoutRevisionNotFound(f"文档 {doc_id} 无解析产物，无法建立版式修订")
        tables = self._read_artifacts(layout_dir / f"{doc_id}_tables.jsonl")
        images = self._read_artifacts(layout_dir / f"{doc_id}_images.jsonl")

        pages = []
        seen_pages: set[int] = set()
        for record in parsed_records:
            page = int(record["page_num"])
            if page in seen_pages:
                continue  # 追加写产物可能重复登记同一页，保留首次出现的解析记录
            seen_pages.add(page)
            regions = []
            for table in [item for item in tables if int(item.get("page", 0)) == page]:
                regions.append({
                    "kind": "table", "bbox": table.get("bbox"),
                    "rows": table.get("rows", 0), "cols": table.get("cols", 0),
                    "header_rows": table.get("header_row", 1),
                    "merged_cells": table.get("merged_cells", 0),
                })
            for image in [item for item in images if int(item.get("page", 0)) == page]:
                regions.append({
                    "kind": "image", "bbox": image.get("bbox"),
                    "language": image.get("language", "chinese"),
                })
            # 解析产物是追加写的，重复解析会重复登记同一区域，按内容去重
            unique: list[dict[str, Any]] = []
            seen_regions: set[str] = set()
            for region in regions:
                key = _dump({"kind": region["kind"], "bbox": _normalize_bbox(region["bbox"]),
                             "rows": region.get("rows"), "cols": region.get("cols")})
                if key not in seen_regions:
                    seen_regions.add(key)
                    unique.append(region)
            regions = unique
            pages.append({
                "page": page,
                "page_status": "待确认",
                "source": {"restored_from": "layout_restore", "source_hash": f"restore-{doc_id}-p{page}"},
                "regions": regions,
            })
        return self.save_document(doc_id, max(page["page"] for page in pages), pages)

    @staticmethod
    def _read_artifacts(path: Path) -> list[dict[str, Any]]:
        if not path.exists():
            return []
        records = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return records

    def save_document(self, doc_id: str, total_pages: int,
                      pages: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
        """初始化整份文档的页集合（仅允许一次），后续变更走 update_page。"""
        with _locked_doc(doc_id):
            if self.load(doc_id) is not None:
                raise LayoutRevisionError(f"文档 {doc_id} 已存在版式修订，请使用更新接口")
            if total_pages < 1:
                raise LayoutRevisionError("总页数必须至少为 1")
            revisions = []
            for raw in pages:
                page = int(raw["page"])
                if not 1 <= page <= total_pages:
                    raise LayoutRevisionError(f"页码 {page} 超出 1-{total_pages}")
                regions = _normalize_regions(page, raw.get("regions") or [])
                status = raw.get("page_status") or raw.get("status") or "待确认"
                if status not in {"待确认", "已确认"}:
                    raise LayoutRevisionError(f"页状态不合法：{status}")
                confirmed_at = raw.get("confirmed_at")
                if status == "已确认" and not confirmed_at:
                    confirmed_at = _now()
                revisions.append(_page_document(doc_id, page, 1, status, confirmed_at,
                                                regions, raw.get("source") or {}))
            seen = {item["page"] for item in revisions}
            missing = sorted(set(range(1, total_pages + 1)) - seen)
            if missing:
                raise LayoutRevisionError(f"缺少页码：{missing}")
            self._commit(doc_id, total_pages, revisions)
            return self.load(doc_id)  # type: ignore[return-value]

    def update_page(self, doc_id: str, page: int, *, regions: list[dict[str, Any]] | None = None,
                    page_status: str | None = None, confirmed_at: str | None = None,
                    source: dict[str, Any] | None = None,
                    expected_revision: int | None = None) -> dict[str, Any]:
        """更新单页区域并生成新修订；其余页保持不变。"""
        with _locked_doc(doc_id):
            pages = self.load(doc_id)
            if pages is None:
                raise LayoutRevisionNotFound(f"文档 {doc_id} 尚无版式修订")
            current = pages.get(page)
            if current is None:
                raise LayoutRevisionNotFound(f"文档 {doc_id} 没有第 {page} 页")
            if expected_revision is not None and int(expected_revision) != current["page_revision"]:
                raise LayoutRevisionError(
                    f"第 {page} 页修订已变为 {current['page_revision']}，"
                    f"期望 {expected_revision}，请重新读取后再更新")
            next_status = page_status or current["page_status"]
            if next_status not in {"待确认", "已确认"}:
                raise LayoutRevisionError(f"页状态不合法：{next_status}")
            next_regions = (_normalize_regions(page, regions) if regions is not None
                            else copy.deepcopy(current["regions"]))
            if next_status == "已确认":
                next_confirmed = confirmed_at or current.get("confirmed_at") or _now()
            else:
                next_confirmed = confirmed_at if confirmed_at is not None else None
            next_source = dict(current.get("source") or {})
            if source:
                next_source.update(source)
            revision = _page_document(doc_id, page, int(current["page_revision"]) + 1,
                                      next_status, next_confirmed, next_regions, next_source)
            self._commit(doc_id, self.total_pages(doc_id), [revision])
            return revision

    def _commit(self, doc_id: str, total_pages: int, revisions: list[dict[str, Any]]) -> None:
        doc_dir = self._doc_dir(doc_id)
        doc_dir.mkdir(parents=True, exist_ok=True)
        pages = self.load(doc_id) or {}
        for revision in revisions:
            target = self._revision_file(doc_id, revision["page"], int(revision["page_revision"]))
            if target.exists():
                raise LayoutRevisionError(f"第 {revision['page']} 页修订文件已存在，拒绝覆盖")
            _write_json_atomic(target, revision)
            pages[revision["page"]] = revision
        payload = {
            "schema": INDEX_SCHEMA,
            "doc_id": doc_id,
            "total_pages": total_pages,
            "pages": [pages[number] for number in sorted(pages)],
            "updated_at": _now(),
        }
        _write_json_atomic(doc_dir / "pages.json", payload)


class LayoutRerunPackage:
    """把一份版式修订快照导出为可离线复跑的 ZIP。"""

    def __init__(self, repo: LayoutRevisionRepo | None = None) -> None:
        self.repo = repo or LayoutRevisionRepo()

    # -- 快照 --

    def snapshot(self, doc_id: str, page_numbers: list[int] | None = None) -> dict[str, Any]:
        """取一次文档页快照。并发更新时，快照内每页都来自同一时刻的修订。"""
        with _locked_doc(doc_id):
            pages = self.repo.load(doc_id)
            if pages is None:
                raise LayoutRevisionNotFound(f"文档 {doc_id} 尚无版式修订")
            selected = sorted(pages) if not page_numbers else sorted(set(page_numbers))
            missing = [number for number in selected if number not in pages]
            if missing:
                raise LayoutRevisionNotFound(f"文档 {doc_id} 缺少页码：{missing}")
            return {
                "doc_id": doc_id,
                "total_pages": len(pages),
                "captured_at": _now(),
                "pages": [copy.deepcopy(pages[number]) for number in selected],
            }

    # -- 包内容 --

    def build(self, doc_id: str, page_numbers: list[int] | None = None,
              *, exported_by: str | None = None) -> dict[str, Any]:
        data = self.snapshot(doc_id, page_numbers)
        pages = data["pages"]
        batch_tables = self._batch_tables(doc_id, pages)
        readiness = self._readiness(pages, data, batch_tables)
        package_id = "LRP-%s-%s" % (doc_id, uuid.uuid4().hex[:10].upper())
        manifest = {
            "schema": MANIFEST_SCHEMA,
            "format_version": PACKAGE_FORMAT_VERSION,
            "package_id": package_id,
            "doc_id": doc_id,
            "created_at": _now(),
            "exported_by": exported_by or "",
            "total_pages_in_document": data["total_pages"],
            "selected_pages": [page["page"] for page in pages],
            "page_count": len(pages),
            "batch_table_count": batch_tables["table_count"],
            "batch_table_pages": batch_tables["pages"],
            "readiness": readiness,
            "entrypoint": "rerun.py",
            "commands": self._commands(page_numbers),
        }
        manifest["manifest_checksum"] = _checksum({key: value for key, value in manifest.items()
                                                   if key != "manifest_checksum"})
        evidence = self._evidence(doc_id, pages, package_id)
        files = {
            "manifest.json": manifest,
            "pages.json": {
                "schema": INDEX_SCHEMA,
                "doc_id": doc_id,
                "package_id": package_id,
                "pages": pages,
            },
            "evidence.json": evidence,
        }
        return {"package_id": package_id, "manifest": manifest, "files": files,
                "readiness": readiness, "batch_tables": batch_tables}

    def _evidence(self, doc_id: str, pages: list[dict[str, Any]],
                  package_id: str) -> dict[str, Any]:
        """逐页、逐区域列出确认依据与校验和，下载后可以核对每个区域来自哪次确认。"""
        items = []
        for page in pages:
            items.append({
                "page": page["page"],
                "page_revision": page["page_revision"],
                "page_status": page["page_status"],
                "confirmed_at": page.get("confirmed_at"),
                "source": page.get("source") or {},
                "page_checksum": page.get("page_checksum"),
                "region_checksum": page.get("region_checksum"),
                "regions": [{
                    "region_id": region["region_id"],
                    "kind": region["kind"],
                    "bbox_text": region.get("bbox_text"),
                    "checksum": _checksum(region),
                } for region in page["regions"]],
            })
        return {
            "schema": EVIDENCE_SCHEMA,
            "package_id": package_id,
            "doc_id": doc_id,
            "pages": items,
            "checksum_rules": {
                "page": "除 created_at、page_checksum 外的页字段做规范化 JSON 的 SHA-256",
                "region": "regions 规范化 JSON 的 SHA-256",
                "manifest": "除 manifest_checksum 外的清单字段做规范化 JSON 的 SHA-256",
            },
        }

    def _commands(self, page_numbers: list[int] | None) -> dict[str, str]:
        pages_arg = ""
        if page_numbers:
            pages_arg = " --pages " + ",".join(str(number) for number in sorted(page_numbers))
        return {
            "verify": "python3 rerun.py verify .",
            "rerun": f"python3 rerun.py rerun . --out ./rerun-output{pages_arg}",
            "extract_tables": f"python3 rerun.py extract-tables . --out ./cells.jsonl{pages_arg}",
            "extract_tables_including_unconfirmed":
                f"python3 rerun.py extract-tables . --out ./cells-all.jsonl "
                f"--include-unconfirmed{pages_arg}",
        }

    def _batch_tables(self, doc_id: str, pages: list[dict[str, Any]]) -> dict[str, Any]:
        """统计当前快照里的表格，并对照批次登记的文件清单判断能否核对整批。"""
        table_pages: dict[int, int] = {}
        for page in pages:
            count = sum(1 for region in page["regions"] if region["kind"] == "table")
            if count:
                table_pages[page["page"]] = count
        batches = []
        for row in store.rows("file_intake"):
            listed = [name.strip() for name in str(row.get("文件清单") or "").split(",") if name.strip()]
            if doc_id in listed:
                batches.append({"batch_key": row.get("批次编号"), "status": row.get("status"),
                                "doc_ids": listed})
        return {
            "table_count": sum(table_pages.values()),
            "pages": table_pages,
            "batches": batches,
        }

    def _readiness(self, pages: list[dict[str, Any]], data: dict[str, Any],
                   batch_tables: dict[str, Any]) -> dict[str, Any]:
        ready = [page["page"] for page in pages if page["page_status"] == "已确认"]
        blocked = [{"page": page["page"], "reason": f"{page['page_status']}（页修订 {page['page_revision']}）"}
                   for page in pages if page["page_status"] != "已确认"]
        document_pages = sorted(int(number) for number in range(1, data["total_pages"] + 1))
        selected = sorted(page["page"] for page in pages)
        missing = [number for number in document_pages if number not in selected]
        covers_whole = not missing
        return {
            "ready_pages": ready,
            "blocked_pages": blocked,
            "selected_pages": selected,
            "missing_pages": missing,
            "covers_whole_document": covers_whole,
            "can_rerun_now": len(blocked) == 0,
            "can_rerun_whole_document": covers_whole and len(blocked) == 0,
            "can_check_batch_tables": covers_whole and len(blocked) == 0 and bool(batch_tables["batches"]),
            "note": self._readiness_note(ready, blocked, missing, batch_tables),
        }

    @staticmethod
    def _readiness_note(ready: list[int], blocked: list[dict[str, Any]],
                        missing: list[int], batch_tables: dict[str, Any]) -> str:
        if not blocked:
            note = f"所选 {len(ready)} 页均已确认，可立即复跑。"
        else:
            blocked_pages = "、".join(str(item["page"]) for item in blocked)
            note = f"第 {blocked_pages} 页尚未确认，不能整包复跑。"
        if missing:
            missing_pages = "、".join(str(number) for number in missing)
            note += f" 包内未包含第 {missing_pages} 页，不能代表整份文档。"
        if not batch_tables["batches"]:
            note += " 当前文档不在任何接入批次清单中，包内表格数不能用于核对批次。"
        elif blocked or missing:
            note += " 表格数只能覆盖已确认且已导出的页，不能用于核对整批表格数。"
        else:
            note += f" 可核对 {len(batch_tables['batches'])} 个批次，共 {batch_tables['table_count']} 张表。"
        return note

    # -- 打包 --

    def write_zip(self, doc_id: str, page_numbers: list[int] | None = None,
                  *, exported_by: str | None = None) -> tuple[Path, dict[str, Any]]:
        """一次快照同时生成包内容和落盘 ZIP，返回落盘路径与包信息。"""
        built = self.build(doc_id, page_numbers, exported_by=exported_by)
        PACKAGES_ROOT.mkdir(parents=True, exist_ok=True)
        target = PACKAGES_ROOT / f"{built['package_id']}.zip"
        payload = self._render_zip(built)
        with _PACKAGE_LOCK:
            tmp = target.with_suffix(".zip.tmp")
            tmp.write_bytes(payload)
            os.replace(tmp, target)
            index = self._load_index()
            index["packages"].append({
                "package_id": built["package_id"],
                "doc_id": doc_id,
                "created_at": built["manifest"]["created_at"],
                "selected_pages": built["manifest"]["selected_pages"],
                "readiness": built["readiness"],
                "file": target.name,
            })
            _write_json_atomic(PACKAGE_INDEX, index)
        return target, built

    def _render_zip(self, built: dict[str, Any]) -> bytes:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            for name in ("manifest.json", "pages.json", "evidence.json"):
                archive.writestr(name, json.dumps(built["files"][name], ensure_ascii=False,
                                                  indent=2, sort_keys=True) + "\n")
            archive.writestr("rerun.py", RUNNER_ASSET.read_text(encoding="utf-8"))
            archive.writestr("README.md", self._readme(built))
        return buffer.getvalue()

    def _readme(self, built: dict[str, Any]) -> str:
        manifest = built["manifest"]
        readiness = built["readiness"]
        lines = [
            "# 版式复跑包",
            "",
            f"- 包编号：`{manifest['package_id']}`",
            f"- 文档编号：`{manifest['doc_id']}`",
            f"- 导出时间：{manifest['created_at']}",
            f"- 页数：{manifest['page_count']} / 文档共 {manifest['total_pages_in_document']} 页",
            "",
            "## 可复跑判断",
            "",
            f"- 可立即复跑的页：{readiness['ready_pages'] or '无'}",
        ]
        for item in readiness["blocked_pages"]:
            lines.append(f"- 第 {item['page']} 页不可复跑：{item['reason']}")
        lines.append(f"- 整份是否可复跑：{'是' if readiness['can_rerun_whole_document'] else '否'}")
        lines.append(f"- 是否覆盖整份文档：{'是' if readiness['covers_whole_document'] else '否'}")
        if readiness["missing_pages"]:
            lines.append(f"- 未包含的页：{readiness['missing_pages']}")
        lines.append(f"- 是否可用于核对批次表格数：{'是' if readiness['can_check_batch_tables'] else '否'}")
        lines.append(f"- 说明：{readiness['note']}")
        lines += [
            "",
            "## 离线复跑",
            "",
            "```bash",
            "python3 rerun.py verify .",
            "python3 rerun.py rerun . --out ./rerun-output",
            "python3 rerun.py extract-tables . --out ./cells.jsonl",
            "```",
            "",
            "`verify` 校验包清单、每页校验和、区域校验和与确认依据是否一致；"
            "`rerun` 按页与区域重新生成版式，未确认页会跳过并标注；"
            "`extract-tables` 默认只抽已确认页，输出逐单元格数据，"
            "带页码、表格号、行列坐标，可与原批次逐页核对；"
            "确需查看未确认页时加 `--include-unconfirmed`。",
            "",
        ]
        return "\n".join(lines)

    def _load_index(self) -> dict[str, Any]:
        if PACKAGE_INDEX.exists():
            return _read_json(PACKAGE_INDEX)
        return {"schema": INDEX_SCHEMA, "packages": []}

    def list_packages(self) -> list[dict[str, Any]]:
        return list(reversed(self._load_index()["packages"]))

    def package_path(self, package_id: str) -> Path | None:
        for item in self._load_index()["packages"]:
            if item["package_id"] == package_id:
                path = PACKAGES_ROOT / item["file"]
                return path if path.exists() else None
        return None

    # -- 消费 --

    def load_for_extraction(self, source: str | Path) -> dict[str, Any]:
        """从复跑包（ZIP 或解压目录）读取页与区域，供表格抽取消费。"""
        path = Path(source)
        if path.is_dir():
            manifest = _read_json(path / "manifest.json")
            pages = _read_json(path / "pages.json")
        elif path.is_file() and zipfile.is_zipfile(path):
            with zipfile.ZipFile(path) as archive:
                manifest = json.loads(archive.read("manifest.json"))
                pages = json.loads(archive.read("pages.json"))
        else:
            raise LayoutRevisionError(f"无法识别的版式复跑包：{path}")
        page_items = pages.get("pages", [])
        if manifest.get("page_count") != len(page_items):
            raise LayoutRevisionError("包清单页数与 pages.json 不一致")
        for page in page_items:
            if _checksum(page["regions"]) != page.get("region_checksum"):
                raise LayoutRevisionError(f"第 {page['page']} 页区域校验和不一致")
        return {"manifest": manifest, "pages": page_items, "source": str(path)}

    def table_regions(self, source: str | Path,
                      page_numbers: list[int] | None = None) -> list[dict[str, Any]]:
        """展平包内表格区域，保留页、页修订、确认依据和区域号，供单元格抽取。"""
        loaded = self.load_for_extraction(source)
        wanted = set(page_numbers) if page_numbers else None
        regions = []
        for page in loaded["pages"]:
            if wanted is not None and page["page"] not in wanted:
                continue
            for region in page["regions"]:
                if region["kind"] != "table":
                    continue
                regions.append({
                    "doc_id": page["doc_id"],
                    "page": page["page"],
                    "page_revision": page["page_revision"],
                    "page_status": page["page_status"],
                    "confirmed_at": page.get("confirmed_at"),
                    "page_checksum": page.get("page_checksum"),
                    "region_id": region["region_id"],
                    "bbox": region["bbox"],
                    "rows": region["rows"],
                    "cols": region["cols"],
                    "header_rows": region.get("header_rows", 1),
                    "merged_cells": region.get("merged_cells", 0),
                    "cells": region.get("cells"),
                })
        return regions
