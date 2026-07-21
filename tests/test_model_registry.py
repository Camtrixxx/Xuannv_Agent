"""Tests for ModelRegistryService: listing, caching, alias matching, status.

Offline tests stub JsonHttpClient. One @live smoke test hits the real API and
is skipped unless AGENT_LIVE_TESTS=1.
"""

from __future__ import annotations

import os

import pytest

from agent.services.model_registry_service import ModelInfo, ModelRegistryService

LIVE = os.getenv("AGENT_LIVE_TESTS") == "1"

# Shape mirrors the real GET /models payload (verified against live service).
CUSTOM_WETLAND = {
    "id": "model_wet1", "name": "湿地头", "type": "single_time_detection",
    "task_type": "building_extraction", "status": "completed", "source": "custom",
    "classes": [{"id": "cls_000", "name": "湿地", "color": "#20beda"}],
}
CUSTOM_WETLAND_TRAINING = {
    "id": "model_wet2", "name": "湿地头v2", "type": "single_time_detection",
    "task_type": "building_extraction", "status": "training", "source": "custom",
    "classes": [{"id": "cls_000", "name": "湿地", "color": "#20beda"}],
}
SYSTEM_BUILDING = {
    "id": "building_extraction", "name": "建筑物提取", "type": "single_time_detection",
    "task_type": "building_extraction", "status": "ready", "source": "system",
    "classes": [{"id": "s0", "name": "背景"}, {"id": "s1", "name": "建筑物"}],
}


def _registry(monkeypatch, models):
    svc = ModelRegistryService(cache_ttl=60.0)
    monkeypatch.setattr(svc.http, "get_list_optional", lambda path: list(models))
    return svc


def test_list_models_parses_payload(monkeypatch):
    svc = _registry(monkeypatch, [CUSTOM_WETLAND, SYSTEM_BUILDING])
    models = svc.list_models("haidian")
    assert {m.id for m in models} == {"model_wet1", "building_extraction"}
    assert all(isinstance(m, ModelInfo) for m in models)


def test_is_ready_and_is_training():
    assert ModelInfo.from_payload(CUSTOM_WETLAND).is_ready
    assert not ModelInfo.from_payload(CUSTOM_WETLAND).is_training
    assert ModelInfo.from_payload(SYSTEM_BUILDING).is_ready  # system "ready"
    assert ModelInfo.from_payload(CUSTOM_WETLAND_TRAINING).is_training


def test_custom_models_filters_out_system(monkeypatch):
    svc = _registry(monkeypatch, [CUSTOM_WETLAND, SYSTEM_BUILDING])
    assert [m.id for m in svc.custom_models("haidian")] == ["model_wet1"]


def test_find_custom_models_matches_class_name(monkeypatch):
    svc = _registry(monkeypatch, [CUSTOM_WETLAND, SYSTEM_BUILDING])
    found = svc.find_custom_models("haidian", "湿地")
    assert [m.id for m in found] == ["model_wet1"]
    assert svc.find_custom_models("haidian", "机场") == []


def test_find_custom_models_ready_sorts_first(monkeypatch):
    svc = _registry(monkeypatch, [CUSTOM_WETLAND_TRAINING, CUSTOM_WETLAND])
    found = svc.find_custom_models("haidian", "湿地")
    assert found[0].id == "model_wet1"  # completed before training


def test_cache_hit_avoids_refetch(monkeypatch):
    svc = ModelRegistryService(cache_ttl=60.0)
    calls = {"n": 0}

    def counting(path):
        calls["n"] += 1
        return [CUSTOM_WETLAND]

    monkeypatch.setattr(svc.http, "get_list_optional", counting)
    svc.list_models("haidian")
    svc.list_models("haidian")
    assert calls["n"] == 1
    svc.invalidate("haidian")
    svc.list_models("haidian")
    assert calls["n"] == 2


def test_empty_class_name_returns_nothing(monkeypatch):
    svc = _registry(monkeypatch, [CUSTOM_WETLAND])
    assert svc.find_custom_models("haidian", "") == []


@pytest.mark.skipif(not LIVE, reason="set AGENT_LIVE_TESTS=1 to hit the live API")
def test_live_list_models_haidian():
    svc = ModelRegistryService()
    models = svc.list_models("haidian")
    assert models, "live /models returned nothing"
    assert any(m.source == "custom" for m in models)
