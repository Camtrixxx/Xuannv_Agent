"""Unit tests for frontend patch-id -> AEF sample index mapping."""

from __future__ import annotations

from agent.services.aef_analysis_service import MockPatchSelector
from agent.services.patch_selection_service import PatchSelectionService
from agent.services.yajiang_patch_index_service import YajiangPatchIndexService


def _indices(patch_ids, count):
    return MockPatchSelector()._selected_patch_indices(patch_ids, count)


def test_patch_prefix_stripped_to_int():
    assert _indices(["patch_000040"], 1) == [40]


def test_plain_integer_id():
    assert _indices(["7"], 1) == [7]


def test_count_limits_results():
    assert _indices(["patch_000040", "patch_000041", "patch_000042"], 2) == [40, 41]


def test_invalid_ids_skipped():
    assert _indices(["patch_abc", "not_a_patch"], 1) == []


def test_negative_index_skipped():
    assert _indices(["patch_-3", "patch_000005"], 1) == [5]


def test_empty_selection():
    assert _indices([], 1) == []


def test_haidian_patch_search_allows_empty_task():
    service = PatchSelectionService()
    patch = {
        "has_embedding": True,
        "available_tasks": ["building_extraction"],
    }
    assert service._task_available("haidian", patch, "") is True


def test_yajiang_patch_index_allows_empty_task():
    service = YajiangPatchIndexService()
    service._patches = [
        {
            "patch_id": "patch_000001",
            "sample_index": 1,
            "bounds_wgs84": [116.0, 39.0, 117.0, 40.0],
            "available_months": ["2025-12"],
            "available_tasks": ["landcover"],
        }
    ]
    rows = service.search([116.1, 39.1, 116.2, 39.2], task_id="", time_range="2025-12", limit=5)
    assert [row["patch_id"] for row in rows] == ["patch_000001"]


def test_search_returns_recoverable_status_for_invalid_bbox():
    result = PatchSelectionService().search(
        {"region": "北京市海淀区", "task": "建筑物提取", "bbox": [116.2, 39.9, 116.2, 39.9]}
    )
    assert result.status == "invalid"
    assert result.patches == []
    assert "矩形范围" in result.message


def test_search_maps_region_alias_and_skips_malformed_patch(monkeypatch):
    service = PatchSelectionService()
    monkeypatch.setattr(
        service,
        "_get_json",
        lambda path: {
            "patches": [
                {"patch_id": "bad", "has_embedding": True, "bounds_wgs84": ["oops"]},
                {
                    "patch_id": "good",
                    "has_embedding": True,
                    "bounds_wgs84": [116.1, 39.8, 116.3, 40.1],
                },
            ],
            "has_next": False,
        },
    )
    result = service.search(
        {"region": "  哈尔滨区域 ", "task": "", "bbox": [116.2, 39.9, 116.25, 39.95]}
    )
    assert result.status == "ok"
    assert result.region_id == "harbin"
    assert [item["patch_id"] for item in result.patches] == ["good"]


def test_search_converts_upstream_failure_to_retryable_status(monkeypatch):
    service = PatchSelectionService()
    monkeypatch.setattr(service, "_search_region", lambda *args: (_ for _ in ()).throw(RuntimeError("down")))
    result = service.search(
        {"region": "北京市海淀区", "task": "建筑物提取", "bbox": [116.2, 39.9, 116.25, 39.95]}
    )
    assert result.status == "retryable_error"
    assert result.selected_patch_ids == []


# ----------------------------------------- Haidian available_tasks under-reports

def _haidian_page(monkeypatch, service, tasks_by_patch):
    """Stub one upstream page of Haidian patches inside the query bbox."""
    monkeypatch.setattr(
        service,
        "_get_json",
        lambda path: {
            "patches": [
                {
                    "patch_id": patch_id,
                    "has_embedding": True,
                    "bounds_wgs84": [116.24, 39.88, 116.29, 39.90],
                    "available_months": ["202512"],
                    "available_tasks": tasks,
                }
                for patch_id, tasks in tasks_by_patch
            ],
            "has_next": False,
        },
    )


def test_haidian_keeps_patches_whose_available_tasks_omit_the_task(monkeypatch):
    """Verified upstream 2026-08: only 62/320 Haidian patches list construction,
    but the result endpoint serves a valid PNG for the others and 404s only for a
    task that truly doesn't exist. Filtering on available_tasks turned a framed
    8-patch selection into 1, which is the "漏选" the user reported.
    """
    service = PatchSelectionService()
    _haidian_page(monkeypatch, service, [
        ("patch_000002", ["building_extraction", "construction"]),
        ("patch_000010", ["building_extraction"]),  # construction not listed
        ("patch_000009", ["building_extraction"]),
    ])

    result = service.search({
        "region": "北京市海淀区", "task": "施工识别",
        "time_range": "2025-12", "bbox": [116.24, 39.88, 116.29, 39.90], "limit": 0,
    })

    assert result.status == "ok"
    assert len(result.patches) == 3


def test_haidian_ranks_patches_that_advertise_the_task_first(monkeypatch):
    # Kept patches are advisory, so a 1-patch pick must still land on real data.
    service = PatchSelectionService()
    _haidian_page(monkeypatch, service, [
        ("patch_000010", ["building_extraction"]),
        ("patch_000002", ["building_extraction", "construction"]),
    ])

    patches = service.search({
        "region": "北京市海淀区", "task": "施工识别",
        "time_range": "2025-12", "bbox": [116.24, 39.88, 116.29, 39.90], "limit": 0,
    }).patches

    assert patches[0]["patch_id"] == "patch_000002"
    assert patches[0]["task_available"] is True
    assert patches[1]["task_available"] is False


def test_month_filter_still_applies_and_explains_the_fallback(monkeypatch):
    """Only task availability became advisory — the month gate still bites.

    A month with no data yields no month-matched patches, so the pre-existing
    Haidian fallback lists them spatially and says why, rather than pretending
    2026-05 was available.
    """
    service = PatchSelectionService()
    _haidian_page(monkeypatch, service, [("patch_000002", ["construction"])])

    assert service._month_available("haidian", {"available_months": ["202512"]}, "2026-05") is False

    result = service.search({
        "region": "北京市海淀区", "task": "施工识别",
        "time_range": "2026-05", "bbox": [116.24, 39.88, 116.29, 39.90], "limit": 0,
    })

    assert "暂无可用海淀 embedding" in result.message
    assert "2025-12" in result.message  # normalized for display


def test_harbin_still_filters_on_task_availability(monkeypatch):
    # The relaxation is Haidian-specific; Harbin's metadata is trustworthy.
    service = PatchSelectionService()
    monkeypatch.setattr(service, "_get_json", lambda path: {
        "patches": [{
            "patch_id": "p1", "has_embedding": True,
            "bounds_wgs84": [126.5, 45.74, 126.57, 45.765],
            "available_months": ["2025-09"],
            "available_tasks": ["land_use_classification"],
        }],
        "has_next": False,
    })

    result = service.search({
        "region": "哈尔滨新区", "task": "建筑物提取",
        "time_range": "2025-09", "bbox": [126.5, 45.74, 126.57, 45.765], "limit": 0,
    })

    assert result.patches == []


def test_patch_without_embedding_is_still_dropped(monkeypatch):
    service = PatchSelectionService()
    monkeypatch.setattr(service, "_get_json", lambda path: {
        "patches": [{
            "patch_id": "p1", "has_embedding": False,
            "bounds_wgs84": [116.24, 39.88, 116.29, 39.90],
            "available_months": ["202512"], "available_tasks": ["construction"],
        }],
        "has_next": False,
    })

    result = service.search({
        "region": "北京市海淀区", "task": "施工识别",
        "time_range": "2025-12", "bbox": [116.24, 39.88, 116.29, 39.90], "limit": 0,
    })

    assert result.patches == []
