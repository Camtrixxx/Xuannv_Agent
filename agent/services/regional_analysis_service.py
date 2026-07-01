from __future__ import annotations

from agent.schemas.report import AnalysisResult, ReportRequest
from agent.services.aef_analysis_service import AEFAnalysisService
from agent.services.haidian_embedding_service import HaidianEmbeddingAnalysisService
from agent.services.harbin_embedding_service import HarbinEmbeddingAnalysisService


class RegionalAnalysisService:
    """Route analysis requests to the proper regional model service."""

    def __init__(
        self,
        yajiang_service: AEFAnalysisService | None = None,
        harbin_service: HarbinEmbeddingAnalysisService | None = None,
        haidian_service: HaidianEmbeddingAnalysisService | None = None,
    ) -> None:
        self.yajiang_service = yajiang_service or AEFAnalysisService()
        self.harbin_service = harbin_service or HarbinEmbeddingAnalysisService()
        self.haidian_service = haidian_service or HaidianEmbeddingAnalysisService()

    def analyze(self, request: ReportRequest) -> AnalysisResult:
        if self._is_harbin(request.region):
            return self.harbin_service.analyze(request)
        if self._is_haidian(request.region):
            return self.haidian_service.analyze(request)
        return self.yajiang_service.analyze(request)

    def _is_harbin(self, region: str) -> bool:
        text = str(region or "")
        return "哈尔滨" in text or text.lower() in {"harbin", "harbin_new_area"}

    def _is_haidian(self, region: str) -> bool:
        text = str(region or "")
        return "海淀" in text or text.lower() in {"haidian", "beijing_haidian"}
