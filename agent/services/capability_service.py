"""Capability gate — can we analyze this object now, or is annotation needed?

Sits in front of ``run_analysis``. Given a region + a target object phrase, it
answers one of four ways so the agent can either proceed, wait, or hand off to
the external annotation UI:

    native           → a system task / land-cover class covers it; proceed
    custom_ready     → a trained custom model exists; proceed with model_id
    custom_training  → a custom model is training; ask the user to wait
    custom_failed    → last training failed; say so and offer a retry
    needs_annotation → nothing exists; hand off to annotate + train

The handoff instruction is enriched from ``GET /models/capabilities`` (the
region's default training method + the task's temporal contract) when reachable,
falling back to built-in defaults otherwise.

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
CUSTOM_FAILED = "custom_failed"
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

    def resolve(
        self,
        region: str,
        target_object: str,
        *,
        model_type: str = "single_time_detection",
        refresh: bool = False,
    ) -> Capability:
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

        matches = self.registry.find_custom_models(
            region_id,
            class_name,
            model_type=model_type,
            use_cache=not refresh,
        )
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
        # A model exists but its last training failed → offer a retry (re-annotate),
        # distinct from "never annotated" so the message can say what happened.
        failed = next((m for m in matches if m.is_failed), None)
        if failed is not None:
            return Capability(
                kind=CUSTOM_FAILED, target_object=obj, class_name=class_name,
                model_id=failed.id, model_status=failed.status, region_id=region_id,
            )
        return Capability(
            kind=NEEDS_ANNOTATION, target_object=obj, class_name=class_name,
            region_id=region_id,
        )

    def detect_custom_object(self, region: str, text: str) -> str:
        """Find a trained custom class named in free text.

        This keeps newly created classes usable without adding every class name
        to the Agent taxonomy. Native objects still win and fixed aliases are
        handled before this method is called by the graph.
        """
        phrase = str(text or "")
        if not phrase or native_object(phrase):
            return ""
        names = {
            name.strip()
            for model in self.registry.custom_models(resolve_region_id(region))
            for name in model.class_names
            if name.strip()
        }
        return next((name for name in sorted(names, key=len, reverse=True) if name in phrase), "")

    def annotation_action(
        self, cap: Capability, *, model_type: str = "single_time_detection", month: str = ""
    ) -> dict[str, Any]:
        """Build the frontend handoff instruction (open the annotation UI).

        The agent returns this in AgentResponse.action; the frontend decides how
        to open it (new tab). Mockable — in debug the UI just opens the URL.

        Enriched from GET /models/capabilities when reachable: the region's real
        default training method and the task's temporal contract are attached so
        the annotation page opens pre-aligned. We do NOT expose a method picker —
        the backend default (xuannv_earth) is used; the field is informational.
        """
        caps = self.registry.capabilities(cap.region_id)
        default_method = str(caps.get("default_training_method") or "xuannv_earth")
        contract_key = "change_detection" if model_type == "change_detection" else "land_use_classification"
        task_contract = (caps.get("task_contracts") or {}).get(contract_key) or {}

        params = {
            "region_id": cap.region_id,
            "class": cap.class_name,
            "model_type": model_type,
            "training_method": default_method,
        }
        if month:
            params["month"] = month
        return {
            "type": "open_annotation_ui",
            "url": f"{self.annotation_ui_base}/models/new?{urlencode(params)}",
            "class_name": cap.class_name,
            "model_type": model_type,
            "training_method": default_method,
            "task_contract": task_contract,
            "params": params,
        }

    def refresh(self, region: str) -> None:
        self.registry.invalidate(resolve_region_id(region))
