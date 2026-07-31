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


def _conversation(service, turns):
    """Record ``turns`` report turns: user ask, assistant reply, report."""
    for index in range(turns):
        service.append_user("s", f"ask {index}")
        service.append_agent("s", "已按你的要求生成新版报告。")
        service.record_report(
            "s",
            title=f"report {index}",
            html_url=f"/reports/{index}.html",
            markdown_url=f"/reports/{index}.md",
            request={},
        )


def test_snapshot_keeps_a_short_window_for_agent_context(tmp_path):
    service = MemoryService(MemoryConfig(db_path=tmp_path / "memory.sqlite3"))
    _conversation(service, turns=5)

    snapshot = service.snapshot("s")

    # The agent's own context stays small; widening it would change every prompt.
    assert len(snapshot["recent_messages"]) == 6
    assert len(snapshot["reports"]) == 5


def test_full_history_snapshot_returns_every_message_and_report(tmp_path):
    """Session restore needs the whole conversation.

    With only the 6-message window, a session with 5 reports loses the turns
    that produced the older ones, so the frontend can't place those report
    cards anywhere but the end.
    """
    service = MemoryService(MemoryConfig(db_path=tmp_path / "memory.sqlite3"))
    _conversation(service, turns=5)

    snapshot = service.snapshot("s", full_history=True)

    assert len(snapshot["recent_messages"]) == 10
    assert len(snapshot["reports"]) == 5
    # Oldest first, so the frontend can merge against report timestamps.
    assert snapshot["recent_messages"][0]["content"] == "ask 0"
    assert snapshot["recent_messages"][-1]["content"] == "已按你的要求生成新版报告。"


def test_full_history_snapshot_is_bounded(tmp_path):
    service = MemoryService(MemoryConfig(db_path=tmp_path / "memory.sqlite3"))
    for index in range(120):
        service.append_user("s", f"m{index}")

    messages = service.snapshot("s", full_history=True)["recent_messages"]

    assert len(messages) == min(120, MemoryService._RESTORE_MESSAGE_LIMIT)
    assert messages[-1]["content"] == "m119"  # newest kept when truncating


def test_every_report_carries_a_timestamp_for_interleaving(tmp_path):
    # The frontend orders report cards against messages purely by created_at.
    service = MemoryService(MemoryConfig(db_path=tmp_path / "memory.sqlite3"))
    _conversation(service, turns=3)

    snapshot = service.snapshot("s", full_history=True)

    assert all(item["created_at"] for item in snapshot["reports"])
    assert all(item["created_at"] for item in snapshot["recent_messages"])


def test_now_is_timezone_aware_utc_with_subsecond_precision():
    value = memory_service._now()
    parsed = datetime.fromisoformat(value)

    assert parsed.utcoffset() is not None
    assert parsed.utcoffset().total_seconds() == 0
    assert "." in value
