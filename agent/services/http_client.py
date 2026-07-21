"""Shared JSON/asset HTTP client for the region data-access layer.

The three region services (Harbin, Haidian, patch selection) previously each
kept a private ``build_opener(ProxyHandler({}))`` + ``_get_json`` + download
copy. They are consolidated here so timeout/retry/proxy behaviour lives in one
place. Each caller passes its own ``error_prefix`` so user-facing error
messages stay exactly as before.

Retry is opt-in (``max_attempts=1`` by default → identical to the old
behaviour); a caller can raise it to ride out transient upstream blips.
"""

from __future__ import annotations

import json
import shutil
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import ProxyHandler, Request, build_opener


class JsonHttpClient:
    def __init__(
        self,
        base_url: str,
        timeout: int,
        error_prefix: str,
        max_attempts: int = 1,
        retry_backoff: float = 0.5,
    ) -> None:
        self.base_url = base_url.rstrip("/") + "/"
        self.timeout = timeout
        self.error_prefix = error_prefix
        self.max_attempts = max(1, max_attempts)
        self.retry_backoff = retry_backoff
        # Empty ProxyHandler bypasses any ambient HTTP(S)_PROXY env vars.
        self.opener = build_opener(ProxyHandler({}))

    def _url(self, path: str) -> str:
        return urljoin(self.base_url, path.lstrip("/"))

    def request_json(self, path: str, method: str = "GET", body: Any = None) -> Any:
        url = self._url(path)
        headers = {"Accept": "application/json"}
        data: bytes | None = None
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = Request(url, data=data, headers=headers, method=method)
        last_exc: Exception | None = None
        for attempt in range(self.max_attempts):
            try:
                with self.opener.open(request, timeout=self.timeout) as response:
                    return json.loads(response.read().decode("utf-8"))
            except (OSError, URLError, json.JSONDecodeError) as exc:
                last_exc = exc
                if attempt + 1 < self.max_attempts:
                    time.sleep(self.retry_backoff * (attempt + 1))
        raise RuntimeError(f"{self.error_prefix}：{url}，原因：{last_exc}") from last_exc

    def get_json(self, path: str) -> Any:
        return self.request_json(path, method="GET")

    def post_json(self, path: str, body: Any = None) -> Any:
        return self.request_json(path, method="POST", body=body)

    def get_json_optional(self, path: str) -> dict[str, Any]:
        try:
            payload = self.get_json(path)
        except RuntimeError:
            return {}
        return payload if isinstance(payload, dict) else {}

    def get_list_optional(self, path: str) -> list[Any]:
        """GET a JSON array, returning [] on any error (e.g. legend endpoints)."""
        try:
            payload = self.get_json(path)
        except RuntimeError:
            return []
        return payload if isinstance(payload, list) else []

    def fetch_bytes(self, remote_url: str, asset_label: str) -> bytes:
        """Fetch a remote asset into memory (no disk write).

        Used when many assets are read transiently (e.g. per-patch result PNGs
        for AOI aggregation) and caching each to ``asset_dir`` would be noise.
        Honours the same proxy/timeout/retry policy as ``request_json``.
        """
        source_url = self._url(remote_url)
        last_exc: Exception | None = None
        for attempt in range(self.max_attempts):
            try:
                with self.opener.open(source_url, timeout=self.timeout) as response:
                    return response.read()
            except (OSError, URLError) as exc:
                last_exc = exc
                if attempt + 1 < self.max_attempts:
                    time.sleep(self.retry_backoff * (attempt + 1))
        raise RuntimeError(f"{asset_label}下载失败：{source_url}，原因：{last_exc}") from last_exc

    def download(self, remote_url: str, out_path: Path, asset_label: str) -> Path:
        source_url = self._url(remote_url)
        if out_path.exists():
            return out_path
        try:
            with self.opener.open(source_url, timeout=self.timeout) as response, out_path.open("wb") as fh:
                shutil.copyfileobj(response, fh)
        except HTTPError as exc:
            raise RuntimeError(f"{asset_label}下载失败：{source_url}，HTTP {exc.code}") from exc
        except OSError as exc:
            raise RuntimeError(f"{asset_label}下载失败：{source_url}，原因：{exc}") from exc
        return out_path
