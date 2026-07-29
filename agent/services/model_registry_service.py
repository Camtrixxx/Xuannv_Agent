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
from agent.taxonomy import FAILED_MODEL_STATUSES, READY_MODEL_STATUSES, TRAINING_MODEL_STATUSES


@dataclass(slots=True)
class ModelInfo:
    id: str
    name: str
    type: str
    task_type: str
    status: str
    source: str  # "system" | "custom"
    classes: list[dict[str, Any]] = field(default_factory=list)
    # Enriched training metadata (present on the updated backend; "" / None when
    # absent, e.g. old or system models).
    resolved_training_method: str = ""   # pu_query_retrieval | binary_conv3x3 | random_forest | pixel_mlp
    requested_training_method: str = ""  # xuannv_earth | traditional_ml | aef | dinov3_sat493m
    feature_source: str = ""             # xuannv_embedding | sentinel2_l2a | aef | dinov3_sat493m
    accuracy: float | None = None
    metric_name: str = ""
    n_samples: int | None = None
    created_at: str = ""
    completed_at: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def uses_annual_feature(self) -> bool:
        """AEF features are per-year: months within one year share an embedding,
        so same-year change detection on such a model is meaningless."""
        return self.feature_source == "aef"

    @property
    def is_ready(self) -> bool:
        return self.status in READY_MODEL_STATUSES

    @property
    def is_training(self) -> bool:
        return self.status in TRAINING_MODEL_STATUSES

    @property
    def is_failed(self) -> bool:
        return self.status in FAILED_MODEL_STATUSES

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
            resolved_training_method=str(payload.get("resolved_training_method") or ""),
            requested_training_method=str(payload.get("requested_training_method") or ""),
            feature_source=str(payload.get("feature_source") or ""),
            accuracy=payload.get("accuracy") if isinstance(payload.get("accuracy"), (int, float)) else None,
            metric_name=str(payload.get("metric_name") or ""),
            n_samples=payload.get("n_samples") if isinstance(payload.get("n_samples"), int) else None,
            created_at=str(payload.get("created_at") or ""),
            completed_at=str(payload.get("completed_at") or ""),
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
        # region_id -> (fetched_at_monotonic, capabilities dict)
        self._cap_cache: dict[str, tuple[float, dict[str, Any]]] = {}

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

    def capabilities(self, region_id: str, *, use_cache: bool = True) -> dict[str, Any]:
        """GET /models/capabilities?region_id= — training methods + task contracts.

        Returns {} on failure so callers can fall back to built-in defaults. The
        payload shape (verified live): {schema_version, default_training_method,
        regions, methods:[{id,name,available,feature_source,supported_model_types,
        selection_rule,...}], task_contracts:{<task>:{temporal_mode,required_fields,
        description,available?}}}.
        """
        from urllib.parse import urlencode

        key = region_id or "_"
        if use_cache:
            hit = self._cap_cache.get(key)
            if hit and (self._now() - hit[0]) < self.cache_ttl:
                return hit[1]
        payload = self.http.get_json_optional(
            f"/models/capabilities?{urlencode({'region_id': region_id})}"
        )
        caps = payload if isinstance(payload, dict) else {}
        self._cap_cache[key] = (self._now(), caps)
        return caps

    def custom_models(self, region_id: str, *, use_cache: bool = True) -> list[ModelInfo]:
        return [m for m in self.list_models(region_id, use_cache=use_cache) if m.source == "custom"]

    def find_custom_models(
        self,
        region_id: str,
        class_name: str,
        *,
        model_type: str = "",
        use_cache: bool = True,
    ) -> list[ModelInfo]:
        """Custom models whose class list matches ``class_name`` (alias/contains).

        Ready models sort first so a caller can pick ``[0]`` for the usable one.
        """
        target = str(class_name or "").strip()
        if not target:
            return []
        out: list[ModelInfo] = []
        for m in self.custom_models(region_id, use_cache=use_cache):
            if model_type and m.type != model_type:
                continue
            names = m.class_names
            if any(target == n or target in n or n in target for n in names if n):
                out.append(m)
        out.sort(key=lambda m: m.created_at, reverse=True)
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
            self._cap_cache.pop(region_id or "_", None)
        else:
            self._cache.clear()
            self._cap_cache.clear()
