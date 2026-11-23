"""版式复跑包：导出、确认依据、并发一致性与离线复跑。

场景对应业务要求：
- 两页已确认、第三页表格坐标刚更正时导出，包应说明哪些页能立即复跑；
- 第三页更正并确认后再导出，包内页与区域应全部来自新修订；
- 旧包重新打开仍能复现当时确认的页；
- 导出与区域更新并发时，包内所选页与其区域保持一致；
- 表格抽取消费复跑包，写出可逐页核对的单元格数据；
- 解压后离线跑包，对照实际页、表格和行列。
"""
from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
import zipfile
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))

from app.services.layout_revision import (  # noqa: E402
    LayoutRevisionError,
    LayoutRevisionRepo,
    LayoutRerunPackage,
)
from app.services.table_extract import TableExtractService  # noqa: E402
from app.store import store  # noqa: E402


def _doc(commit_pages=(1, 2), third_bbox=(300, 240, 520, 330), third_status="待确认"):
    """构造三页文档：1、2 页已确认，第 3 页按参数决定坐标与确认状态。"""
    return {
        "total_pages": 3,
        "pages": [
            {
                "page": 1,
                "page_status": "已确认",
                "confirmed_at": "2026-11-20T09:00:00+00:00",
                "source": {"confirmed_by": "版式复核-甲", "source_hash": "hash-p1"},
                "regions": [
                    {"kind": "paragraph", "bbox": [40, 30, 560, 200], "reading_order": 0},
                    {"kind": "table", "bbox": [50, 210, 540, 320], "rows": 3, "cols": 4,
                     "header_rows": 1, "merged_cells": 0},
                ],
            },
            {
                "page": 2,
                "page_status": "已确认",
                "confirmed_at": "2026-11-20T09:05:00+00:00",
                "source": {"confirmed_by": "版式复核-乙", "source_hash": "hash-p2"},
                "regions": [
                    {"kind": "paragraph", "bbox": [40, 30, 560, 180], "reading_order": 0},
                    {"kind": "image", "bbox": [60, 190, 260, 320], "language": "chinese", "reading_order": 1},
                ],
            },
            {
                "page": 3,
                "page_status": third_status,
                "confirmed_at": ("2026-11-21T10:00:00+00:00"
                                 if third_status == "已确认" else None),
                "source": {"confirmed_by": "版式复核-丙", "source_hash": "hash-p3-r1"},
                "regions": [
                    {"kind": "paragraph", "bbox": [40, 30, 560, 150], "reading_order": 0},
                    {"kind": "table", "bbox": list(third_bbox), "rows": 4, "cols": 5,
                     "header_rows": 1, "merged_cells": 2},
                ],
            },
        ],
    }


@pytest.fixture()
def repo(tmp_path, monkeypatch):
    import app.services.layout_revision as module

    monkeypatch.setattr(module, "REVISIONS_ROOT", tmp_path / "revisions")
    monkeypatch.setattr(module, "PACKAGES_ROOT", tmp_path / "packages")
    monkeypatch.setattr(module, "PACKAGE_INDEX", tmp_path / "packages" / "index.json")
    return LayoutRevisionRepo()


@pytest.fixture()
def doc_id():
    return "DEMO-LAYOUT-001"


def _unzip(path: Path, target: Path) -> Path:
    target.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path) as archive:
        archive.extractall(target)
    return target


def _run_offline(package_dir: Path, *args: str) -> subprocess.CompletedProcess:
    """在解压目录里用隔离模式运行包内脚本，确保离线包不依赖后端代码。"""
    return subprocess.run(
        [sys.executable, "-I", "rerun.py", *args],
        cwd=str(package_dir),
        capture_output=True,
        text=True,
        timeout=60,
    )


class TestPackageBeforeCorrection:
    def test_readiness_marks_only_confirmed_pages(self, repo, doc_id):
        repo.save_document(doc_id, 3, _doc()["pages"])
        package = LayoutRerunPackage(repo)
        built = package.build(doc_id)

        readiness = built["readiness"]
        assert readiness["ready_pages"] == [1, 2]
        assert [item["page"] for item in readiness["blocked_pages"]] == [3]
        assert readiness["can_rerun_now"] is False
        assert readiness["can_check_batch_tables"] is False
        assert "第 3 页尚未确认" in readiness["note"]
        # 第三页表格仍在包内，但带有当时的旧坐标，并明确未确认
        page3 = next(page for page in built["files"]["pages.json"]["pages"] if page["page"] == 3)
        assert page3["page_status"] == "待确认"
        assert page3["page_revision"] == 1
        assert page3["regions"][1]["bbox"] == [300.0, 240.0, 520.0, 330.0]

    def test_partial_export_not_whole_document(self, repo, doc_id):
        repo.save_document(doc_id, 3, _doc(third_status="已确认")["pages"])
        built = LayoutRerunPackage(repo).build(doc_id, [1, 2])
        readiness = built["readiness"]
        assert readiness["selected_pages"] == [1, 2]
        assert readiness["missing_pages"] == [3]
        assert readiness["covers_whole_document"] is False
        assert readiness["can_rerun_now"] is True
        assert readiness["can_rerun_whole_document"] is False
        assert readiness["can_check_batch_tables"] is False
        assert "未包含第 3 页" in readiness["note"]

    def test_every_region_has_evidence(self, repo, doc_id):
        repo.save_document(doc_id, 3, _doc()["pages"])
        built = LayoutRerunPackage(repo).build(doc_id)
        evidence = built["files"]["evidence.json"]
        assert evidence["schema"] == "layout-rerun/evidence/v1"
        page_entries = {item["page"]: item for item in evidence["pages"]}
        assert set(page_entries) == {1, 2, 3}
        for page in built["files"]["pages.json"]["pages"]:
            entry = page_entries[page["page"]]
            assert entry["page_checksum"] == page["page_checksum"]
            assert entry["region_checksum"] == page["region_checksum"]
            assert entry["page_revision"] == page["page_revision"]
            assert len(entry["regions"]) == len(page["regions"])
            for region in page["regions"]:
                match = next(item for item in entry["regions"]
                             if item["region_id"] == region["region_id"])
                assert match["kind"] == region["kind"]
                assert match["bbox_text"] == region["bbox_text"]

    def test_batch_tables_cover_only_selected_pages(self, repo, doc_id):
        store.rows("file_intake").append({
            "id": 9001, "status": "已接入", "pending": False, "abnormal": False,
            "批次编号": "BATCH-RERUN-1", "来源系统": "测试", "文件清单": f"{doc_id},OTHER-DOC",
            "文件类型": "PDF", "字节总量": "1", "校验值": "", "采集方式": "接口", "接入游标": "0",
        })
        try:
            repo.save_document(doc_id, 3, _doc()["pages"])
            built = LayoutRerunPackage(repo).build(doc_id)
            assert built["manifest"]["batch_table_pages"] == {1: 1, 3: 1}
            assert built["manifest"]["batch_table_count"] == 2
            assert built["readiness"]["can_check_batch_tables"] is False
        finally:
            store.rows("file_intake").remove(
                next(row for row in store.rows("file_intake") if row["id"] == 9001))


class TestPackageAfterCorrection:
    def test_corrected_page_uses_new_revision_only(self, repo, doc_id):
        repo.save_document(doc_id, 3, _doc()["pages"])
        old = LayoutRerunPackage(repo).build(doc_id)

        repo.update_page(doc_id, 3, page_status="已确认",
                         confirmed_at="2026-11-22T08:00:00+00:00",
                         regions=[
                             {"kind": "paragraph", "bbox": [40, 30, 560, 150], "reading_order": 0},
                             {"kind": "table", "bbox": [320, 260, 560, 360], "rows": 4, "cols": 5,
                              "header_rows": 1, "merged_cells": 2},
                         ],
                         source={"confirmed_by": "版式复核-丙", "source_hash": "hash-p3-r2"})
        new = LayoutRerunPackage(repo).build(doc_id)

        assert new["readiness"]["ready_pages"] == [1, 2, 3]
        assert new["readiness"]["can_rerun_now"] is True
        page3_new = next(page for page in new["files"]["pages.json"]["pages"] if page["page"] == 3)
        assert page3_new["page_revision"] == 2
        assert page3_new["regions"][1]["bbox"] == [320.0, 260.0, 560.0, 360.0]
        assert page3_new["page_checksum"] != old["files"]["pages.json"]["pages"][2]["page_checksum"]

        # 旧包仍复现更正前确认过的页，两包互不影响
        pages1_new = {page["page"]: page for page in new["files"]["pages.json"]["pages"]}
        pages1_old = {page["page"]: page for page in old["files"]["pages.json"]["pages"]}
        assert pages1_new[1] == pages1_old[1]
        assert pages1_new[2] == pages1_old[2]
        assert pages1_old[3]["page_revision"] == 1

        # 包内每一页只来自一个修订，不会拼页
        saved = repo.load(doc_id)
        for page in new["files"]["pages.json"]["pages"]:
            assert page["page_revision"] == saved[page["page"]]["page_revision"]
            assert page["region_checksum"] == saved[page["page"]]["region_checksum"]
            assert page["page_checksum"] == saved[page["page"]]["page_checksum"]

    def test_old_package_reopens_with_original_pages(self, repo, doc_id):
        repo.save_document(doc_id, 3, _doc()["pages"])
        old_path, _ = LayoutRerunPackage(repo).write_zip(doc_id)

        repo.update_page(doc_id, 3, page_status="已确认",
                         regions=[{"kind": "table", "bbox": [320, 260, 560, 360],
                                   "rows": 4, "cols": 5}],
                         source={"source_hash": "hash-p3-r2"})
        new_path, _ = LayoutRerunPackage(repo).write_zip(doc_id)
        assert old_path != new_path

        package = LayoutRerunPackage(repo)
        old_pages = package.load_for_extraction(old_path)["pages"]
        new_pages = package.load_for_extraction(new_path)["pages"]
        old3 = next(page for page in old_pages if page["page"] == 3)
        new3 = next(page for page in new_pages if page["page"] == 3)
        assert old3["page_revision"] == 1 and old3["page_status"] == "待确认"
        assert old3["regions"][1]["bbox"] == [300.0, 240.0, 520.0, 330.0]
        assert new3["page_revision"] == 2 and new3["page_status"] == "已确认"
        assert new3["regions"][0]["bbox"] == [320.0, 260.0, 560.0, 360.0]

        # 同一份包重复打开，内容完全一致
        assert old_pages == package.load_for_extraction(old_path)["pages"]


class TestConcurrentExport:
    def test_export_stays_consistent_with_updates(self, repo, doc_id):
        """导出与区域更新并发：每一页的页记录和区域必须来自同一修订。"""
        repo.save_document(doc_id, 3, _doc()["pages"])
        package = LayoutRerunPackage(repo)
        observed: list[dict] = []
        failures: list[str] = []
        stop = threading.Event()

        def updater():
            revision = 1
            while not stop.is_set() and revision < 30:
                revision += 1
                try:
                    repo.update_page(
                        doc_id, 3,
                        regions=[{"kind": "table",
                                  "bbox": [300 + revision, 240 + revision, 520 + revision, 330 + revision],
                                  "rows": 4, "cols": 5}],
                        source={"revision_marker": revision})
                except LayoutRevisionError as exc:  # pragma: no cover - 线程内错误
                    failures.append(str(exc))
                    return
                time.sleep(0.002)

        worker = threading.Thread(target=updater)
        worker.start()
        try:
            for _ in range(40):
                data = package.snapshot(doc_id)
                page3 = next(page for page in data["pages"] if page["page"] == 3)
                table = next(region for region in page3["regions"] if region["kind"] == "table")
                x0 = table["bbox"][0]
                # 首轮 bbox 为 300+1，之后每次更新 revision 与 x0 同步递增
                if page3["page_revision"] > 1 and x0 != 300 + page3["page_revision"]:
                    failures.append(
                        f"页修订 {page3['page_revision']} 与区域 {table['bbox']} 不同步")
                observed.append(page3)
                time.sleep(0.002)
        finally:
            stop.set()
            worker.join(timeout=10)

        assert not failures, failures
        revisions = {page["page_revision"] for page in observed}
        assert len(revisions) > 1, "并发更新期间应观察到多个页修订，才能验证快照隔离"

    def test_zip_export_is_snapshot_isolated(self, repo, doc_id):
        repo.save_document(doc_id, 3, _doc()["pages"])
        package = LayoutRerunPackage(repo)
        paths: list[Path] = []

        def exporter():
            for _ in range(5):
                paths.append(package.write_zip(doc_id)[0])

        workers = [threading.Thread(target=exporter) for _ in range(2)]
        updater_done = threading.Event()

        def updater():
            for revision in range(2, 8):
                repo.update_page(
                    doc_id, 3,
                    regions=[{"kind": "table", "bbox": [300 + revision, 240, 520, 330],
                              "rows": 4, "cols": 5}],
                    source={"revision_marker": revision})
            updater_done.set()

        update_thread = threading.Thread(target=updater)
        for worker in workers:
            worker.start()
        update_thread.start()
        for worker in workers:
            worker.join(timeout=20)
        update_thread.join(timeout=20)
        assert updater_done.is_set()
        assert len(paths) == 10

        for path in paths:
            loaded = package.load_for_extraction(path)  # 内含页与区域校验和校验
            page3 = next(page for page in loaded["pages"] if page["page"] == 3)
            table = next(region for region in page3["regions"] if region["kind"] == "table")
            assert table["bbox"][0] == (300 if page3["page_revision"] == 1
                                        else 300 + page3["page_revision"])
            # 页校验和覆盖其区域，拼页会立刻校验失败
            assert page3["page_revision"] >= 1


class TestOfflineRerun:
    def test_offline_verify_rerun_and_extract(self, repo, doc_id, tmp_path):
        repo.save_document(doc_id, 3, _doc(third_status="已确认")["pages"])
        path, _ = LayoutRerunPackage(repo).write_zip(doc_id)
        workdir = _unzip(path, tmp_path / "unpacked")

        verify = _run_offline(workdir, "verify", ".")
        assert verify.returncode == 0, verify.stderr
        report = json.loads(verify.stdout)
        assert report["ok"] is True
        assert report["ready_pages"] == [1, 2, 3]
        assert [page["table_count"] for page in report["pages"]] == [1, 0, 1]

        rerun = _run_offline(workdir, "rerun", ".", "--out", "./rerun-output")
        assert rerun.returncode == 0, rerun.stderr
        summary = json.loads(rerun.stdout)
        assert summary["pages_rerun"] == 3
        layout_rows = [json.loads(line) for line in
                       (workdir / "rerun-output" / "layout.jsonl").read_text().splitlines()]
        assert [row["page"] for row in layout_rows] == [1, 2, 3]
        assert layout_rows[2]["page_revision"] == 1
        assert layout_rows[2]["confirmed_at"] == "2026-11-21T10:00:00+00:00"

        extract = _run_offline(workdir, "extract-tables", ".", "--out", "./cells.jsonl")
        assert extract.returncode == 0, extract.stderr
        stats = json.loads(extract.stdout)
        assert stats["tables"] == 2
        assert stats["cells"] == 3 * 4 + 4 * 5
        cells = [json.loads(line) for line in (workdir / "cells.jsonl").read_text().splitlines()]
        page3_cells = [cell for cell in cells if cell["page"] == 3]
        assert len(page3_cells) == 20
        assert {cell["row"] for cell in page3_cells} == {0, 1, 2, 3}
        assert {cell["col"] for cell in page3_cells} == {0, 1, 2, 3, 4}
        assert all(cell["table_region_id"] == "p003-table-001" for cell in page3_cells)

    def test_offline_verify_rejects_tampered_package(self, repo, doc_id, tmp_path):
        repo.save_document(doc_id, 3, _doc(third_status="已确认")["pages"])
        path, _ = LayoutRerunPackage(repo).write_zip(doc_id)
        workdir = _unzip(path, tmp_path / "unpacked")
        pages_file = workdir / "pages.json"
        payload = json.loads(pages_file.read_text())
        payload["pages"][0]["regions"][0]["bbox"] = [0, 0, 10, 10]
        pages_file.write_text(json.dumps(payload, ensure_ascii=False))

        verify = _run_offline(workdir, "verify", ".")
        assert verify.returncode == 1
        report = json.loads(verify.stdout)
        assert report["ok"] is False
        assert any("第 1 页" in error for error in report["errors"])

    def test_extract_tables_skips_unconfirmed_selection(self, repo, doc_id, tmp_path):
        repo.save_document(doc_id, 3, _doc()["pages"])
        path, _ = LayoutRerunPackage(repo).write_zip(doc_id)
        workdir = _unzip(path, tmp_path / "unpacked")

        selected = _run_offline(workdir, "rerun", ".", "--out", "./rerun-confirmed",
                                "--pages", "1,2")
        assert selected.returncode == 0, selected.stderr
        summary = json.loads(selected.stdout)
        assert summary["pages_rerun"] == 2
        assert summary["pages_skipped"] == []


class TestTableExtractionFromPackage:
    def test_cells_trace_back_to_page_and_region(self, repo, doc_id, tmp_path):
        repo.save_document(doc_id, 3, _doc(third_status="已确认")["pages"])
        path, _ = LayoutRerunPackage(repo).write_zip(doc_id)

        result = TableExtractService().extract_from_package(path, out_dir=str(tmp_path / "cells"))
        assert result["tables_extracted"] == 2
        assert result["cells_written"] == 32
        assert result["pages"] == [1, 3]
        assert result["cells_per_page"]["1"] == {"tables": 1, "cells": 12}
        assert result["cells_per_page"]["3"] == {"tables": 1, "cells": 20}

        records = [json.loads(line) for line in Path(result["output"]).read_text().splitlines()]
        for record in records:
            assert record["page"] in (1, 3)
            assert record["page_status"] == "已确认"
            assert record["confirmed_at"]
            assert record["page_checksum"]
            assert record["table_region_id"] == f"p{record['page']:03d}-table-001"
            assert 0 <= record["row"] < record["table_rows"]
            assert 0 <= record["col"] < record["table_cols"]

    def test_extraction_respects_requested_pages(self, repo, doc_id, tmp_path):
        repo.save_document(doc_id, 3, _doc(third_status="已确认")["pages"])
        path, _ = LayoutRerunPackage(repo).write_zip(doc_id)
        result = TableExtractService().extract_from_package(
            path, page_numbers=[3], out_dir=str(tmp_path / "cells"))
        assert result["pages"] == [3]
        assert result["cells_written"] == 20

    def test_corrected_region_changes_cells_but_not_other_pages(self, repo, doc_id, tmp_path):
        repo.save_document(doc_id, 3, _doc()["pages"])
        before = TableExtractService().extract_from_package(
            LayoutRerunPackage(repo).write_zip(doc_id)[0], out_dir=str(tmp_path / "before"))

        # 更正前，第 3 页未确认：默认抽取跳过它，只抽已确认的第 1 页
        assert before["pages"] == [1]
        assert before["skipped_unconfirmed_pages"] == [3]

        # 显式要求包含未确认页时，仍能拿到旧修订的单元格
        before_all = TableExtractService().extract_from_package(
            LayoutRerunPackage(repo).write_zip(doc_id)[0], out_dir=str(tmp_path / "before-all"),
            include_unconfirmed=True)
        assert before_all["cells_per_page"]["3"] == {"tables": 1, "cells": 20}
        old_p3 = [json.loads(line) for line in Path(before_all["output"]).read_text().splitlines()
                  if json.loads(line)["page"] == 3]
        assert all(record["page_revision"] == 1 for record in old_p3)

        repo.update_page(doc_id, 3, page_status="已确认",
                         regions=[{"kind": "table", "bbox": [320, 260, 560, 360],
                                   "rows": 2, "cols": 3}],
                         source={"source_hash": "hash-p3-r2"})
        after = TableExtractService().extract_from_package(
            LayoutRerunPackage(repo).write_zip(doc_id)[0], out_dir=str(tmp_path / "after"))

        assert after["cells_per_page"]["3"] == {"tables": 1, "cells": 6}
        assert before["cells_per_page"]["1"] == after["cells_per_page"]["1"]
        after_records = [json.loads(line) for line in Path(after["output"]).read_text().splitlines()]
        page3 = [record for record in after_records if record["page"] == 3]
        assert all(record["page_revision"] == 2 for record in page3)
        assert all(record["table_rows"] == 2 and record["table_cols"] == 3 for record in page3)


class TestRevisionGuards:
    def test_expected_revision_rejects_stale_writer(self, repo, doc_id):
        repo.save_document(doc_id, 3, _doc()["pages"])
        repo.update_page(doc_id, 3, page_status="已确认", expected_revision=1,
                         regions=[{"kind": "table", "bbox": [320, 260, 560, 360],
                                   "rows": 4, "cols": 5}])
        with pytest.raises(LayoutRevisionError, match="修订已变为"):
            repo.update_page(doc_id, 3, regions=[], expected_revision=1)

    def test_revisions_are_append_only(self, repo, doc_id):
        repo.save_document(doc_id, 3, _doc()["pages"])
        repo.update_page(doc_id, 3, page_status="已确认")
        history = repo.revisions(doc_id, 3)
        assert [item["page_revision"] for item in history] == [1, 2]
        assert history[0]["page_status"] == "待确认"
        assert history[1]["page_status"] == "已确认"

    def test_invalid_bbox_rejected(self, repo, doc_id):
        payload = _doc()
        payload["pages"][0]["regions"][1]["bbox"] = [500, 400, 100, 200]
        with pytest.raises(LayoutRevisionError, match="右下角"):
            repo.save_document(doc_id, 3, payload["pages"])

    def test_missing_pages_rejected(self, repo, doc_id):
        with pytest.raises(LayoutRevisionError, match="缺少页码"):
            repo.save_document(doc_id, 4, _doc()["pages"])


class TestPipelineSeeding:
    def test_seed_from_parse_and_layout_artifacts(self, repo, doc_id):
        from app.services.format_parse import FormatParseService

        task, _ = FormatParseService().create_entry({
            "任务编号": "PR-RERUN-SEED", "文档编号": doc_id, "解析器类型": "PDF",
        })
        assert FormatParseService().execute_parse(task["id"])["pages"] == 3

        pages = repo.seed_from_artifacts(doc_id)
        assert sorted(pages) == [1, 2, 3]
        for page in pages.values():
            assert page["page_status"] == "待确认"
            assert page["source"]["restored_from"] == "layout_restore"
        assert [region["kind"] for region in pages[1]["regions"]] == ["table"]
        assert [region["kind"] for region in pages[2]["regions"]] == ["image"]
        assert [region["kind"] for region in pages[3]["regions"]] == ["table"]

        # 从产物起包时，三页都未确认，不能整批复跑
        built = LayoutRerunPackage(repo).build(doc_id)
        assert built["readiness"]["ready_pages"] == []
        assert built["readiness"]["can_rerun_whole_document"] is False


class TestApiEndpoints:
    def test_package_flow_via_api(self, repo, doc_id, monkeypatch):
        from fastapi.testclient import TestClient
        import app.main as main_module
        import app.routers.layout_rerun as router_module

        monkeypatch.setattr(router_module, "repo", repo)
        monkeypatch.setattr(router_module, "packages", __import__(
            "app.services.layout_revision", fromlist=["LayoutRerunPackage"]
        ).LayoutRerunPackage(repo))
        client = TestClient(main_module.app)

        created = client.post(f"/api/layout-rerun/documents/{doc_id}/pages", json=_doc())
        assert created.status_code == 200, created.text
        assert created.json()["pages"] == [1, 2, 3]

        update = client.put(f"/api/layout-rerun/documents/{doc_id}/pages/3", json={
            "page_status": "已确认",
            "regions": [{"kind": "table", "bbox": [320, 260, 560, 360], "rows": 4, "cols": 5}],
            "source": {"source_hash": "hash-p3-r2"},
            "expected_revision": 1,
        })
        assert update.status_code == 200, update.text
        assert update.json()["page_revision"] == 2

        history = client.get(f"/api/layout-rerun/documents/{doc_id}/pages/3/revisions").json()
        assert [item["page_status"] for item in history["revisions"]] == ["待确认", "已确认"]

        created_pkg = client.post("/api/layout-rerun/packages", json={"doc_id": doc_id})
        assert created_pkg.status_code == 200, created_pkg.text
        body = created_pkg.json()
        assert body["readiness"]["can_rerun_whole_document"] is True

        download = client.get(body["download_url"])
        assert download.status_code == 200
        assert download.headers["content-type"] == "application/zip"
        assert len(download.content) > 0

        verified = client.get(f"/api/layout-rerun/packages/{body['package_id']}/verify")
        assert verified.status_code == 200
        assert verified.json()["manifest"]["page_count"] == 3

    def test_generic_module_route_not_shadowed(self):
        from fastapi.testclient import TestClient
        import app.main as main_module

        client = TestClient(main_module.app)
        assert client.get("/api/layout_restore").status_code == 200
        assert client.get("/api/table_extract").status_code == 200
