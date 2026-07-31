"""The debug UI's session-restore timeline merge (runs the real JS under node).

Reports and messages reach the frontend as two separate arrays, so restoring a
session means merging them by ``created_at``. This used to guess the pairing by
string-matching the assistant reply for "报告已生成", which silently failed for
revision turns ("已按你的要求生成新版报告。") and pushed every report card to the
bottom of the conversation. These tests pin the ordering behaviour instead.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess

import pytest

from pathlib import Path

UI_FILE = Path(__file__).resolve().parents[1] / "agent" / "ui" / "agent_dashboard_mock.html"
FUNCTIONS = ("function eventTime", "function dedupeReports", "function buildSessionTimeline")

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not available")


def _extract(source: str, header: str) -> str:
    """Return one JS function body by brace-matching from its declaration."""
    start = source.index(header)
    depth = 0
    for index in range(source.index("{", start), len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[start : index + 1]
    raise AssertionError(f"unbalanced braces in {header}")


def _timeline(memory: dict) -> list[dict]:
    """Run the UI's own buildSessionTimeline over ``memory`` and return events."""
    source = UI_FILE.read_text(encoding="utf-8")
    script = "\n".join(_extract(source, header) for header in FUNCTIONS)
    script += (
        "\nconst events = buildSessionTimeline(JSON.parse(process.env.MEMORY_JSON));"
        "\nconsole.log(JSON.stringify(events.map((e) => ({"
        "  type: e.type,"
        "  label: e.type === 'message' ? e.message.content : e.report.title"
        "}))));"
    )
    proc = subprocess.run(
        ["node", "-e", script],
        capture_output=True, text=True, timeout=30,
        env={**os.environ, "MEMORY_JSON": json.dumps(memory)},
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def _memory(turns: int, reply: str = "已按你的要求生成新版报告。") -> dict:
    """A session of report turns: ask → reply → report, one second apart."""
    messages, reports = [], []
    for index in range(turns):
        base = 10 + index * 10
        messages.append({"role": "user", "content": f"ask{index}",
                         "created_at": f"2026-07-31T03:50:{base:02d}+00:00"})
        messages.append({"role": "assistant", "content": reply,
                         "created_at": f"2026-07-31T03:50:{base + 2:02d}+00:00"})
        reports.append({"title": f"report{index}", "html_url": f"/r{index}.html",
                        "markdown_url": "", "created_at": f"2026-07-31T03:50:{base + 3:02d}+00:00"})
    # The API serves reports newest-first while messages are oldest-first.
    return {"recent_messages": messages, "reports": list(reversed(reports))}


def test_reports_interleave_with_the_turns_that_produced_them():
    events = _timeline(_memory(turns=3))

    assert [item["label"] for item in events] == [
        "ask0", "已按你的要求生成新版报告。", "report0",
        "ask1", "已按你的要求生成新版报告。", "report1",
        "ask2", "已按你的要求生成新版报告。", "report2",
    ]


def test_revision_replies_are_not_matched_by_wording():
    """A reply that never says 报告已生成 must still get its card in place.

    This is the exact regression: revision turns reply "已按你的要求生成新版报告。",
    so the old substring match found no anchor and appended all cards at the end.
    """
    events = _timeline(_memory(turns=2, reply="已按你的要求生成新版报告。"))

    assert [item["type"] for item in events] == ["message", "message", "report"] * 2
    # Nothing bunched at the tail.
    assert events[-1]["label"] == "report1"


def test_first_generation_wording_still_interleaves():
    events = _timeline(_memory(turns=2, reply="报告已生成。"))

    assert [item["type"] for item in events] == ["message", "message", "report"] * 2


def test_report_card_follows_the_reply_it_announces_on_a_timestamp_tie():
    memory = {
        "recent_messages": [
            {"role": "user", "content": "ask", "created_at": "2026-07-31T03:50:10+00:00"},
            {"role": "assistant", "content": "报告已生成。", "created_at": "2026-07-31T03:50:12+00:00"},
        ],
        "reports": [{"title": "report", "html_url": "/r.html", "markdown_url": "",
                     "created_at": "2026-07-31T03:50:12+00:00"}],
    }

    assert [item["label"] for item in _timeline(memory)] == ["ask", "报告已生成。", "report"]


def test_report_without_timestamp_sinks_to_the_end():
    # Unplaceable, but it must not jump ahead of the conversation.
    memory = _memory(turns=1)
    memory["reports"].append({"title": "undated", "html_url": "/u.html",
                              "markdown_url": "", "created_at": ""})

    assert _timeline(memory)[-1]["label"] == "undated"


def test_duplicate_report_entries_render_once():
    memory = _memory(turns=1)
    memory["reports"] = memory["reports"] * 3

    labels = [item["label"] for item in _timeline(memory)]

    assert labels.count("report0") == 1


def test_session_with_reports_but_no_messages_still_lists_them():
    memory = {"recent_messages": [], "reports": [
        {"title": "only", "html_url": "/o.html", "markdown_url": "",
         "created_at": "2026-07-31T03:50:10+00:00"},
    ]}

    assert [item["label"] for item in _timeline(memory)] == ["only"]


def test_empty_memory_yields_no_events():
    assert _timeline({}) == []
