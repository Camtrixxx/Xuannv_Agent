from __future__ import annotations

from agent.schemas.report import AnalysisResult, ReportRequest
from agent.services.aef_analysis_service import AEFAnalysisService
from agent.services.harbin_embedding_service import HarbinEmbeddingAnalysisService


class RegionalAnalysisService:
    """Route analysis requests to the proper regional model service."""

    def __init__(
        self,
        yajiang_service: AEFAnalysisService | None = None,
        harbin_service: HarbinEmbeddingAnalysisService | None = None,
    ) -> None:
        self.yajiang_service = yajiang_service or AEFAnalysisService()
        self.harbin_service = harbin_service or HarbinEmbeddingAnalysisService()

    def analyze(self, request: ReportRequest) -> AnalysisResult:
        if self._is_harbin(request.region):
            return self.harbin_service.analyze(request)
        return self.yajiang_service.analyze(request)

    def _is_harbin(self, region: str) -> bool:
        text = str(region or "")
        return "哈尔滨" in text or text.lower() in {"harbin", "harbin_new_area"}
