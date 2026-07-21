"""Capability gate — can we analyze this object now, or is annotation needed?

Sits in front of ``run_analysis``. Given a region + a target object phrase, it
answers one of four ways so the agent can either proceed, wait, or hand off to
the external annotation UI:

    native           → a system task / land-cover class covers it; proceed
    custom_ready     → a trained custom model exists; proceed with model_id
    custom_training  → a custom model is training; ask the user to wait
    needs_annotation → nothing exists; hand off to annotate + train

The agent NEVER annotates or trains here — it only resolves state (read-only
``GET /models``) and builds the handoff instruction. See
``docs/开发计划-自定义地物标注与训练能力.md``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode

from agent.services.model_registry_service import ModelInfo, ModelRegistryService
from agent.taxonomy import native_object, non_native_object, resolve_region_id

NATIVE = "native"
CUSTOM_READY = "custom_ready"
CUSTOM_TRAINING = "custom_training"
NEEDS_ANNOTATION = "needs_annotation"


@dataclass(slots=True)
class Capability:
    kind: str
    target_object: str = ""
    class_name: str = ""  # canonical custom class name (for training/matching)
    model_id: str = ""
    model_status: str = ""
    region_id: str = ""
    debug: dict[str, Any] = field(default_factory=dict)

    @property
    def is_native(self) -> bool:
        return self.kind == NATIVE

    @property
    def can_proceed(self) -> bool:
        return self.kind in {NATIVE, CUSTOM_READY}


class CapabilityService:
    """Resolve native/custom capability for a requested analysis object."""

    def __init__(
        self,
        registry: ModelRegistryService | None = None,
        annotation_ui_base: str | None = None,
    ) -> None:
        self.registry = registry or ModelRegistryService()
        self.annotation_ui_base = (
            annotation_ui_base
            or os.getenv("AGENT_ANNOTATION_UI_BASE", "http://60.31.21.42:22065")
        ).rstrip("/")

    def resolve(self, region: str, target_object: str) -> Capability:
        """Classify a target object into one of the four capability kinds.

        Native objects short-circuit without touching the network. Only
        genuinely non-native phrases trigger a /models lookup.
        """
        region_id = resolve_region_id(region)
        obj = str(target_object or "").strip()

        # 1) Native? (system task or land-cover class) — no network needed.
        #    Native wins over custom so "水体" uses the system task even if a
        #    custom "水体" model happens to exist; only genuinely non-native
        #    phrases (湿地/机场/…) fall through to custom resolution.
        if native_object(obj):
            return Capability(kind=NATIVE, target_object=obj, region_id=region_id)

        # 2) Non-native — does a custom model already cover this class?
        class_name = non_native_object(obj) or obj
        if not obj:
            # No object identified at all → treat as native (ordinary flow).
            return Capability(kind=NATIVE, target_object="", region_id=region_id)

        matches = self.registry.find_custom_models(region_id, class_name)
        ready = next((m for m in matches if m.is_ready), None)
        if ready is not None:
            return Capability(
                kind=CUSTOM_READY, target_object=obj, class_name=class_name,
                model_id=ready.id, model_status=ready.status, region_id=region_id,
            )
        training = next((m for m in matches if m.is_training), None)
        if training is not None:
            return Capability(
                kind=CUSTOM_TRAINING, target_object=obj, class_name=class_name,
                model_id=training.id, model_status=training.status, region_id=region_id,
            )
        return Capability(
            kind=NEEDS_ANNOTATION, target_object=obj, class_name=class_name,
            region_id=region_id,
        )

    def annotation_action(
        self, cap: Capability, *, model_type: str = "single_time_detection", month: str = ""
    ) -> dict[str, Any]:
        """Build the frontend handoff instruction (open the annotation UI).

        The agent returns this in AgentResponse.action; the frontend decides how
        to open it (new tab). Mockable — in debug the UI just opens the URL.
        """
        params = {
            "region_id": cap.region_id,
            "class": cap.class_name,
            "model_type": model_type,
        }
        if month:
            params["month"] = month
        return {
            "type": "open_annotation_ui",
            "url": f"{self.annotation_ui_base}/models/new?{urlencode(params)}",
            "class_name": cap.class_name,
            "model_type": model_type,
            "params": params,
        }

    def refresh(self, region: str) -> None:
        self.registry.invalidate(resolve_region_id(region))
