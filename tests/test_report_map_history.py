from __future__ import annotations

from agent.config import MemoryConfig
from agent.services.memory_service import MemoryService


def test_report_history_preserves_standalone_map_url(tmp_path):
    service = MemoryService(MemoryConfig(db_path=tmp_path / "memory.sqlite3"))
    service.record_report(
        "session_map",
        title="海淀水体提取报告",
        html_url="/reports/report.html",
        markdown_url="/reports/report.md",
        map_html_url="/reports/report.map.html",
        request={"region": "北京市海淀区", "task": "水体提取"},
    )

    reports = service.recent_reports("session_map")

    assert reports[0]["map_html_url"] == "/reports/report.map.html"
