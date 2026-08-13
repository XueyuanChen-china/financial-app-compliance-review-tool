"""Parallel review pipeline contracts and execution components."""

from compliance_review.review.context import (
    CompressedReviewMemory,
    ContextBudgetExceeded,
    ReviewerContextManager,
    ReviewerContextState,
    ReviewerRuntimeConfig,
)
from compliance_review.review.manifest import ReviewManifestBuilder
from compliance_review.review.provider import (
    ModelProvider,
    OpenAICompatibleProvider,
    StaticModelProvider,
)
from compliance_review.review.scheduler import ReviewScheduler

__all__ = [
    "ModelProvider",
    "OpenAICompatibleProvider",
    "ReviewManifestBuilder",
    "LangGraphReviewRuntime",
    "FullReviewError",
    "FullReviewService",
    "CompressedReviewMemory",
    "ContextBudgetExceeded",
    "ReviewerContextManager",
    "ReviewerContextState",
    "ReviewerRuntimeConfig",
    "ReviewScheduler",
    "StaticModelProvider",
]


def __getattr__(name: str) -> object:
    """Avoid initializing review services while setup/persistence models are loading."""
    if name in {"FullReviewError", "FullReviewService"}:
        from compliance_review.review.full_review import FullReviewError, FullReviewService

        return {"FullReviewError": FullReviewError, "FullReviewService": FullReviewService}[name]
    if name == "LangGraphReviewRuntime":
        from compliance_review.review.langgraph_runtime import LangGraphReviewRuntime

        return LangGraphReviewRuntime
    raise AttributeError(name)
