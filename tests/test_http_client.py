"""Unit tests for the shared JsonHttpClient (data-access layer)."""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from agent.services.http_client import JsonHttpClient


class _FakeResp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


def _client(**kwargs) -> JsonHttpClient:
    return JsonHttpClient(
        base_url="http://example.test/api",
        timeout=3,
        error_prefix="海淀 embedding-api 调用失败",
        **kwargs,
    )


def test_get_json_parses_payload(monkeypatch):
    client = _client()
    monkeypatch.setattr(
        client.opener, "open", lambda req, timeout=None: _FakeResp(json.dumps({"ok": 1}).encode("utf-8"))
    )
    assert client.get_json("/regions/haidian/patches") == {"ok": 1}


def test_url_joins_onto_base_path():
    # base_url keeps its path segment; leading slash on the arg is stripped.
    assert _client()._url("/regions/x") == "http://example.test/api/regions/x"


def test_error_message_keeps_prefix_and_url(monkeypatch):
    client = _client()

    def boom(req, timeout=None):
        raise OSError("refused")

    monkeypatch.setattr(client.opener, "open", boom)
    with pytest.raises(RuntimeError) as excinfo:
        client.get_json("/regions/haidian/patches")
    msg = str(excinfo.value)
    assert msg.startswith("海淀 embedding-api 调用失败：")
    assert "原因：refused" in msg


def test_get_json_optional_swallows_errors(monkeypatch):
    client = _client()
    monkeypatch.setattr(client.opener, "open", lambda req, timeout=None: (_ for _ in ()).throw(OSError("x")))
    assert client.get_json_optional("/whatever") == {}


def test_retry_then_success(monkeypatch):
    client = _client(max_attempts=3, retry_backoff=0)
    calls = {"n": 0}

    def flaky(req, timeout=None):
        calls["n"] += 1
        if calls["n"] < 3:
            raise OSError("transient")
        return _FakeResp(json.dumps({"ok": True}).encode("utf-8"))

    monkeypatch.setattr(client.opener, "open", flaky)
    assert client.get_json("/x") == {"ok": True}
    assert calls["n"] == 3


def test_download_skips_when_file_exists(tmp_path: Path):
    out = tmp_path / "cached.png"
    out.write_bytes(b"already-here")
    # opener.open would raise if called; existing file must short-circuit.
    assert _client().download("/img.png", out, asset_label="海淀专题图像") == out
    assert out.read_bytes() == b"already-here"
