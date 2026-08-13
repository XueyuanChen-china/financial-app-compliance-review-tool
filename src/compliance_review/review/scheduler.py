from __future__ import annotations

from pathlib import Path
from typing import Callable, Mapping

from compliance_review.domain.models import WorkItem
from compliance_review.repository import RepositorySandbox
from compliance_review.review.langgraph_runtime import LangGraphReviewRuntime
from compliance_review.review.models import ReviewRunSummary
from compliance_review.review.provider import ModelProvider

ProviderFactory = Callable[[WorkItem], ModelProvider]


class ReviewScheduler:
    """Backward-compatible facade over the LangGraph review runtime."""

    def __init__(
        self,
        provider: ModelProvider | None = None,
        provider_factory: ProviderFactory | None = None,
        max_concurrency: int = 3,
        token_budget: int = 4000,
        checkpointer: object | None = None,
    ) -> None:
        self.runtime = LangGraphReviewRuntime(
            provider=provider,
            provider_factory=provider_factory,
            max_concurrency=max_concurrency,
            token_budget=token_budget,
            checkpointer=checkpointer,
        )

    def run(
        self,
        manifest_run_id: str,
        work_items: list[WorkItem],
        sandboxes: Mapping[str, RepositorySandbox],
        output_root: Path,
        event_log_path: Path | None = None,
    ) -> ReviewRunSummary:
        return self.runtime.run(
            manifest_run_id=manifest_run_id,
            work_items=work_items,
            sandboxes=sandboxes,
            output_root=output_root,
            event_log_path=event_log_path,
        )
