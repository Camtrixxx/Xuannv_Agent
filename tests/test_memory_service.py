from __future__ import annotations

from datetime import datetime

from agent.config import MemoryConfig
from agent.services import memory_service
from agent.services.memory_service import MemoryService


def test_session_list_uses_stable_newest_first_order_for_timestamp_ties(tmp_path, monkeypatch):
    fixed_time = "2026-07-27T02:00:00.000000+00:00"
    monkeypatch.setattr(memory_service, "_now", lambda: fixed_time)
    service = MemoryService(MemoryConfig(db_path=tmp_path / "memory.sqlite3"))

    service.append_user("session_older", "first")
    service.append_user("session_newer", "second")

    assert [item["session_id"] for item in service.list_sessions()] == ["session_newer", "session_older"]


def test_now_is_timezone_aware_utc_with_subsecond_precision():
    value = memory_service._now()
    parsed = datetime.fromisoformat(value)

    assert parsed.utcoffset() is not None
    assert parsed.utcoffset().total_seconds() == 0
    assert "." in value
