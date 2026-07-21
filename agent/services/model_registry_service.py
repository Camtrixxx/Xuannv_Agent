"""Model registry — the linchpin for custom-object capability resolution.

Wraps the embedding-api ``/models`` endpoints so the agent can answer:
  * which system + custom models exist for a region,
  * whether a named object (e.g. "湿地") already has a *ready* custom model,
  * the live status of a training job / model (for resume-after-training).

Read-only and lightly cached (short TTL) — a custom model, once trained, does
not change, and this list is polled on the capability-gate hot path.

API facts (verified against the live service, see docs/API.md):
  GET /models?region_id=<id>  -> [ {id,name,type,task_type,status,classes,
                                    source, ...}, ... ]  (system + custom)
  GET /models/{model_id}      -> single model object (system id also works)
  GET /models/jobs/{job_id}   -> {status: running|completed|failed, ...}
                                 NOTE: purged after completion → 404, so resume
                                 prefers model status over job status.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from agent.config import EmbeddingAPIConfig
from agent.services.http_client import JsonHttpClient
from agent.taxonomy import READY_MODEL_STATUSES, TRAINING_MODEL_STATUSES


@dataclass(slots=True)
class ModelInfo:
    id: str
    name: str
    type: str
    task_type: str
    status: str
    source: str  # "system" | "custom"
    classes: list[dict[str, Any]] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def is_ready(self) -> bool:
        return self.status in READY_MODEL_STATUSES

    @property
    def is_training(self) -> bool:
        return self.status in TRAINING_MODEL_STATUSES

    @property
    def class_names(self) -> list[str]:
        return [str(c.get("name") or "") for c in self.classes if c.get("name")]

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "ModelInfo":
        return cls(
            id=str(payload.get("id") or ""),
            name=str(payload.get("name") or ""),
            type=str(payload.get("type") or ""),
            task_type=str(payload.get("task_type") or ""),
            status=str(payload.get("status") or ""),
            source=str(payload.get("source") or ""),
            classes=list(payload.get("classes") or []),
            raw=payload,
        )


class ModelRegistryService:
    """Query system + custom models for a region, with a short-TTL cache."""

    def __init__(
        self,
        config: EmbeddingAPIConfig | None = None,
        cache_ttl: float = 60.0,
    ) -> None:
        self.config = config or EmbeddingAPIConfig()
        self.cache_ttl = cache_ttl
        self.http = JsonHttpClient(
            base_url=self.config.base_url,
            timeout=self.config.timeout,
            error_prefix="模型列表查询失败",
        )
        # region_id -> (fetched_at_monotonic, [ModelInfo])
        self._cache: dict[str, tuple[float, list[ModelInfo]]] = {}

    def _now(self) -> float:
        import time

        return time.monotonic()

    def list_models(self, region_id: str, *, use_cache: bool = True) -> list[ModelInfo]:
        """All models (system + custom) visible for a region. [] on failure."""
        key = region_id or "_"
        if use_cache:
            hit = self._cache.get(key)
            if hit and (self._now() - hit[0]) < self.cache_ttl:
                return hit[1]
        from urllib.parse import urlencode

        raw = self.http.get_list_optional(f"/models?{urlencode({'region_id': region_id})}")
        models = [ModelInfo.from_payload(m) for m in raw if isinstance(m, dict)]
        self._cache[key] = (self._now(), models)
        return models

    def custom_models(self, region_id: str) -> list[ModelInfo]:
        return [m for m in self.list_models(region_id) if m.source == "custom"]

    def find_custom_models(self, region_id: str, class_name: str) -> list[ModelInfo]:
        """Custom models whose class list matches ``class_name`` (alias/contains).

        Ready models sort first so a caller can pick ``[0]`` for the usable one.
        """
        target = str(class_name or "").strip()
        if not target:
            return []
        out: list[ModelInfo] = []
        for m in self.custom_models(region_id):
            names = m.class_names
            if any(target == n or target in n or n in target for n in names if n):
                out.append(m)
        out.sort(key=lambda m: (not m.is_ready, not m.is_training))
        return out

    def model_status(self, model_id: str, region_id: str = "") -> ModelInfo | None:
        """Live single-model lookup (works for custom + system ids)."""
        from urllib.parse import urlencode

        path = f"/models/{model_id}"
        if region_id:
            path += f"?{urlencode({'region_id': region_id})}"
        payload = self.http.get_json_optional(path)
        if not payload or not payload.get("id"):
            return None
        return ModelInfo.from_payload(payload)

    def job_status(self, job_id: str) -> dict[str, Any]:
        """Training job status. Empty dict if not found (jobs are purged when done)."""
        return self.http.get_json_optional(f"/models/jobs/{job_id}")

    def invalidate(self, region_id: str = "") -> None:
        if region_id:
            self._cache.pop(region_id or "_", None)
        else:
            self._cache.clear()
