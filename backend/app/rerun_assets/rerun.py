#!/usr/bin/env python3
"""版式复跑包离线执行器（仅依赖 Python 标准库）。

用法：
    python3 rerun.py verify .
    python3 rerun.py rerun . --out ./rerun-output [--pages 1,2]
    python3 rerun.py extract-tables . --out ./cells.jsonl [--pages 1,2]

包内 pages.json 的每一页是一个不可变修订，自带页校验和、区域校验和
与确认依据。本脚本不访问后端、不读取包外数据，排查结果只取决于包内容。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

FORMAT_VERSION = "1.0"
PAGE_SCHEMA = "layout-rerun/page/v1"


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _checksum(value: Any) -> str:
    return hashlib.sha256(_dump(value).encode("utf-8")).hexdigest()


def _page_checksum(page: dict[str, Any]) -> str:
    return _checksum({key: value for key, value in page.items()
                      if key not in {"created_at", "page_checksum"}})


def _load(package_dir: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    manifest = json.loads((package_dir / "manifest.json").read_text(encoding="utf-8"))
    pages_payload = json.loads((package_dir / "pages.json").read_text(encoding="utf-8"))
    return manifest, pages_payload.get("pages", [])


def _selected(pages: list[dict[str, Any]], raw: str | None) -> list[dict[str, Any]]:
    if not raw:
        return pages
    wanted = {int(item) for item in raw.split(",") if item.strip()}
    return [page for page in pages if int(page["page"]) in wanted]


def verify(package_dir: Path) -> dict[str, Any]:
    errors: list[str] = []
    try:
        manifest, pages = _load(package_dir)
    except (OSError, json.JSONDecodeError) as exc:
        return {"ok": False, "errors": [f"包内容无法读取：{exc}"], "pages": []}

    if manifest.get("format_version") != FORMAT_VERSION:
        errors.append(f"包格式版本 {manifest.get('format_version')!r} 不受支持")
    recorded = manifest.get("manifest_checksum")
    recomputed = _checksum({key: value for key, value in manifest.items()
                            if key != "manifest_checksum"})
    if recorded != recomputed:
        errors.append("manifest.json 校验和不一致，清单可能被改动")
    if manifest.get("page_count") != len(pages):
        errors.append(f"清单页数 {manifest.get('page_count')} 与包内页数 {len(pages)} 不一致")
    if sorted(page["page"] for page in pages) != sorted(manifest.get("selected_pages") or []):
        errors.append("清单所选页码与包内页码不一致")
    total = int(manifest.get("total_pages_in_document") or 0)
    selected = sorted(int(page["page"]) for page in pages)
    missing_pages = [number for number in range(1, total + 1) if number not in selected] if total else []

    report = []
    seen_pages: set[int] = set()
    for page in pages:
        number = int(page["page"])
        page_errors: list[str] = []
        if number in seen_pages:
            page_errors.append("同一页在包内出现多次")
        seen_pages.add(number)
        if page.get("schema") != PAGE_SCHEMA:
            page_errors.append(f"页结构版本异常：{page.get('schema')!r}")
        if page.get("page_revision", 0) < 1:
            page_errors.append("页修订号不合法")
        if _checksum(page.get("regions", [])) != page.get("region_checksum"):
            page_errors.append("区域校验和不一致")
        if _page_checksum(page) != page.get("page_checksum"):
            page_errors.append("页校验和不一致")
        status = page.get("page_status")
        if status == "已确认" and not page.get("confirmed_at"):
            page_errors.append("已确认页缺少确认时间")
        if page.get("doc_id") != manifest.get("doc_id"):
            page_errors.append("页的文档编号与清单不一致")
        for region in page.get("regions", []):
            if region.get("region_id") != f"p{number:03d}-{region.get('kind')}-{region.get('seq', 0):03d}":
                page_errors.append(f"区域 {region.get('region_id')} 编号与页码不符")
                break
        if page_errors:
            errors.extend(f"第 {number} 页：{message}" for message in page_errors)
        report.append({
            "page": number,
            "page_revision": page.get("page_revision"),
            "page_status": status,
            "confirmed_at": page.get("confirmed_at"),
            "region_count": len(page.get("regions", [])),
            "table_count": sum(1 for region in page.get("regions", []) if region.get("kind") == "table"),
            "ok": not page_errors,
        })

    return {
        "ok": not errors,
        "package_id": manifest.get("package_id"),
        "doc_id": manifest.get("doc_id"),
        "errors": errors,
        "partial_package": bool(missing_pages),
        "missing_pages": missing_pages,
        "pages": report,
        "ready_pages": [item["page"] for item in report if item["page_status"] == "已确认"],
    }


def rerun(package_dir: Path, out: Path, pages_arg: str | None = None) -> dict[str, Any]:
    manifest, pages = _load(package_dir)
    pages = _selected(pages, pages_arg)
    out.mkdir(parents=True, exist_ok=True)
    layout_rows = []
    results = []
    for page in pages:
        number = int(page["page"])
        confirmed = page.get("page_status") == "已确认"
        tables = [region for region in page.get("regions", []) if region.get("kind") == "table"]
        images = [region for region in page.get("regions", []) if region.get("kind") == "image"]
        layout_rows.append({
            "doc_id": page["doc_id"],
            "page": number,
            "page_revision": page["page_revision"],
            "page_status": page.get("page_status"),
            "paragraph_regions": [region["region_id"] for region in page.get("regions", [])
                                  if region.get("kind") in {"paragraph", "text"}],
            "table_regions": [{
                "region_id": region["region_id"],
                "bbox": region.get("bbox_text"),
                "rows": region.get("rows"),
                "cols": region.get("cols"),
            } for region in tables],
            "image_regions": [region["region_id"] for region in images],
            "reading_order": [region["region_id"] for region in page.get("regions", [])],
            "confirmed_at": page.get("confirmed_at"),
            "rerunnable": confirmed,
        })
        results.append({"page": number, "page_revision": page["page_revision"],
                        "status": "已复跑" if confirmed else "跳过（未确认）"})
    layout_path = out / "layout.jsonl"
    layout_path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in layout_rows),
                           encoding="utf-8")
    summary = {
        "package_id": manifest.get("package_id"),
        "doc_id": manifest.get("doc_id"),
        "pages_selected": len(pages),
        "pages_rerun": sum(1 for item in results if item["status"] == "已复跑"),
        "pages_skipped": [item["page"] for item in results if item["status"] != "已复跑"],
        "results": results,
        "output": str(layout_path),
    }
    (out / "report.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
                                     encoding="utf-8")
    return summary


def _cell_value(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, (str, int, float, bool)):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _table_cells(page: dict[str, Any], region: dict[str, Any]) -> list[dict[str, Any]]:
    rows = int(region.get("rows") or 0)
    cols = int(region.get("cols") or 0)
    explicit = region.get("cells")
    cells = []
    for r in range(rows):
        for c in range(cols):
            if isinstance(explicit, list) and r < len(explicit) and c < len(explicit[r]):
                value = _cell_value(explicit[r][c])
            elif r == 0:
                value = f"列{c + 1}"
            else:
                value = f"值{r}_{c + 1}"
            cells.append({
                "doc_id": page["doc_id"],
                "page": page["page"],
                "page_revision": page["page_revision"],
                "page_status": page.get("page_status"),
                "confirmed_at": page.get("confirmed_at"),
                "page_checksum": page.get("page_checksum"),
                "table_region_id": region["region_id"],
                "table_bbox": region.get("bbox_text"),
                "table_rows": rows,
                "table_cols": cols,
                "row": r,
                "col": c,
                "rowspan": 1,
                "colspan": 1,
                "is_header": r < int(region.get("header_rows", 1) or 1),
                "cell_value": value,
            })
    return cells


def extract_tables(package_dir: Path, out: Path, pages_arg: str | None = None,
                   include_unconfirmed: bool = False) -> dict[str, Any]:
    """输出逐单元格数据；默认只抽已确认页，未确认页单列为 skipped。"""
    manifest, pages = _load(package_dir)
    pages = _selected(pages, pages_arg)
    records = []
    table_count = 0
    skipped: list[int] = []
    for page in pages:
        if page.get("page_status") != "已确认" and not include_unconfirmed:
            skipped.append(int(page["page"]))
            continue
        for region in page.get("regions", []):
            if region.get("kind") != "table":
                continue
            table_count += 1
            records.extend(_table_cells(page, region))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in records),
                   encoding="utf-8")
    per_page: dict[int, int] = {}
    for row in records:
        per_page[row["page"]] = per_page.get(row["page"], 0) + 1
    return {
        "package_id": manifest.get("package_id"),
        "doc_id": manifest.get("doc_id"),
        "pages": len(pages),
        "extracted_pages": sorted(per_page),
        "skipped_unconfirmed_pages": skipped,
        "tables": table_count,
        "cells": len(records),
        "cells_per_page": per_page,
        "output": str(out),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="版式复跑包离线执行器")
    parser.add_argument("command", choices=["verify", "rerun", "extract-tables"])
    parser.add_argument("package", help="解压后的复跑包目录")
    parser.add_argument("--out", default=None, help="rerun 输出目录或 extract-tables 输出文件")
    parser.add_argument("--pages", default=None, help="逗号分隔的页码，默认全部")
    parser.add_argument("--include-unconfirmed", action="store_true",
                        help="extract-tables 时连未确认页一起抽取（默认跳过）")
    args = parser.parse_args(argv)

    package_dir = Path(args.package)
    if args.command == "verify":
        result = verify(package_dir)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["ok"] else 1
    if args.command == "rerun":
        out = Path(args.out or "rerun-output")
        result = rerun(package_dir, out, args.pages)
    else:
        out = Path(args.out or "cells.jsonl")
        result = extract_tables(package_dir, out, args.pages, args.include_unconfirmed)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
