from __future__ import annotations

import hashlib
import json
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Callable, Mapping

from compliance_review.domain.models import ControlSurfaceResult, ReviewResult, Surface, WorkItem
from compliance_review.repository import RepositorySandbox
from compliance_review.review.events import AppendOnlyEventLog
from compliance_review.review.models import ReviewRunSummary, WorkerExecution
from compliance_review.review.provider import ModelProvider
from compliance_review.review.worker import ReviewWorker

ProviderFactory = Callable[[WorkItem], ModelProvider]


class ReviewScheduler:
    """Run independent Work Items with a bounded thread pool."""

    def __init__(
        self,
        provider: ModelProvider | None = None,
        provider_factory: ProviderFactory | None = None,
        max_concurrency: int = 3,
        token_budget: int = 4000,
    ) -> None:
        if (provider is None) == (provider_factory is None):
            raise ValueError("provide exactly one provider or provider_factory")
        if not 1 <= max_concurrency <= 32:
            raise ValueError("max_concurrency must be between 1 and 32")
        self.provider = provider
        self.provider_factory = provider_factory
        self.max_concurrency = max_concurrency
        self.token_budget = token_budget

    def run(
        self,
        manifest_run_id: str,
        work_items: list[WorkItem],
        sandboxes: Mapping[Surface, RepositorySandbox],
        output_root: Path,
        event_log_path: Path | None = None,
    ) -> ReviewRunSummary:
        output_root.mkdir(parents=True, exist_ok=True)
        log_path = event_log_path or output_root.parent / "worker-events.jsonl"
        event_log = AppendOnlyEventLog(log_path)
        event_log.append(
            "run_started",
            {
                "run_id": manifest_run_id,
                "work_item_count": len(work_items),
                "max_concurrency": self.max_concurrency,
            },
        )
        executions: list[WorkerExecution] = []
        with ThreadPoolExecutor(max_workers=self.max_concurrency) as executor:
            futures: dict[Future[WorkerExecution], WorkItem] = {
                executor.submit(
                    self._run_one,
                    manifest_run_id,
                    work_item,
                    sandboxes,
                    output_root,
                    event_log,
                    index,
                ): work_item
                for index, work_item in enumerate(work_items, start=1)
            }
            for future in as_completed(futures):
                work_item = futures[future]
                try:
                    executions.append(future.result())
                except Exception as exc:  # configuration errors stay visible per Work Item
                    executions.append(
                        self._scheduler_failure(
                            manifest_run_id,
                            work_item,
                            output_root,
                            event_log,
                            str(exc),
                        )
                    )
        executions.sort(key=lambda execution: execution.work_item_id)
        event_log.append(
            "run_completed",
            {
                "run_id": manifest_run_id,
                "completed": sum(
                    execution.execution_status == "completed" for execution in executions
                ),
                "failed": sum(
                    execution.execution_status == "failed" for execution in executions
                ),
            },
        )
        return ReviewRunSummary(
            run_id=manifest_run_id,
            executions=executions,
            max_concurrency=self.max_concurrency,
            completed=sum(
                execution.execution_status == "completed" for execution in executions
            ),
            failed=sum(execution.execution_status == "failed" for execution in executions),
            event_log_path=log_path.as_posix(),
        )

    @staticmethod
    def _scheduler_failure(
        run_id: str,
        work_item: WorkItem,
        output_root: Path,
        event_log: AppendOnlyEventLog,
        error: str,
    ) -> WorkerExecution:
        attempt_id = f"{run_id}.{work_item.work_item_id}.scheduler-error"
        result = ReviewResult(
            contract="review_result.v1",
            work_item_id=work_item.work_item_id,
            attempt_id=attempt_id,
            execution_status="failed",
            rows=[
                ControlSurfaceResult(
                    control_id=control_id,
                    surface=work_item.surface,
                    evidence_status="missing",
                    recommended_control_status="indeterminate",
                    gap_reasons=[error],
                )
                for control_id in work_item.control_ids
            ],
            agent_id="scheduler",
            errors=[error],
        )
        result_path = output_root / work_item.work_item_id / "review-result.json"
        result_path.parent.mkdir(parents=True, exist_ok=True)
        result_path.write_text(result.model_dump_json(indent=2), encoding="utf-8")
        fingerprint = hashlib.sha256(
            json.dumps(
                {"run_id": run_id, "work_item": work_item.model_dump()},
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()[:16]
        event_log.append(
            "worker_failed",
            {
                "run_id": run_id,
                "work_item_id": work_item.work_item_id,
                "agent_id": "scheduler",
                "attempt_id": attempt_id,
                "error": error,
                "result_path": result_path.as_posix(),
            },
        )
        return WorkerExecution(
            work_item_id=work_item.work_item_id,
            attempt_id=attempt_id,
            agent_id="scheduler",
            execution_status="failed",
            result_path=result_path.as_posix(),
            result=result,
            context_fingerprint=fingerprint,
            error=error,
        )

    def _run_one(
        self,
        run_id: str,
        work_item: WorkItem,
        sandboxes: Mapping[Surface, RepositorySandbox],
        output_root: Path,
        event_log: AppendOnlyEventLog,
        index: int,
    ) -> WorkerExecution:
        sandbox = sandboxes.get(work_item.surface)
        if sandbox is None:
            raise ValueError(f"missing sandbox for surface: {work_item.surface}")
        provider = self.provider_factory(work_item) if self.provider_factory else self.provider
        if provider is None:
            raise ValueError("provider is not configured")
        worker = ReviewWorker(
            run_id=run_id,
            work_item=work_item,
            agent_id=f"reviewer-{index:03d}",
            provider=provider,
            sandbox=sandbox,
            output_root=output_root,
            event_log=event_log,
            token_budget=self.token_budget,
        )
        return worker.run()
