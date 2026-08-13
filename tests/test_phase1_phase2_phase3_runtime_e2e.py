from __future__ import annotations

import json
from pathlib import Path

from compliance_review.compilation.service import Phase2CompilationService
from compliance_review.review import LangGraphReviewRuntime
from compliance_review.review.models import ModelRequest, ModelResponse, ToolCall
from compliance_review.review.provider import StaticModelProvider
from compliance_review.setup.models import WorkspaceRepository
from compliance_review.setup.service import ReviewSetupService

FIXTURES = Path(__file__).parent / "fixtures" / "day2"


def test_phase1_phase2_phase3_runtime_handoff(tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspace"
    policy = tmp_path / "policy.md"
    policy.write_text(
        "# Loan disclosure\n\nLoan terms must be disclosed before approval.\n",
        encoding="utf-8",
    )

    phase1_service = ReviewSetupService(workspace_root)
    phase1 = phase1_service.initialize(
        [WorkspaceRepository(repo_id="web", path=(FIXTURES / "frontend").as_posix())]
    )
    assert phase1.confirmation.status == "awaiting_confirmation"
    phase1_service.confirm_profile(
        {
            "app_name": "Example Loan",
            "package_name": "com.example.loan",
            "jurisdiction": "Pakistan",
            "business_type": ["personal_loan"],
            "self_lending": True,
        }
    )

    def compilation_response(request: ModelRequest) -> ModelResponse:
        payload = json.loads(request.messages[1]["content"])
        if request.request_kind == "obligation_extraction":
            source = payload["sources"][0]
            section = source["sections"][0]["section_id"]
            return ModelResponse(
                content=json.dumps(
                    {
                        "contract": "obligation_set.v1",
                        "version": "1.0",
                        "status": "draft",
                        "obligations": [
                            {
                                "obligation_id": "obl.loan.disclosure",
                                "source_id": source["source_id"],
                                "source_section": section,
                                "statement": "Loan terms must be disclosed before approval.",
                                "concepts": ["loan", "disclosure"],
                                "applicability_expression": "business_type includes personal_loan",
                                "required_surfaces": ["frontend_h5"],
                                "source_refs": [
                                    {
                                        "source_id": source["source_id"],
                                        "source_section": section,
                                    }
                                ],
                            }
                        ],
                    }
                )
            )
        obligation = payload["obligations"][0]
        return ModelResponse(
            content=json.dumps(
                {
                    "contract": "control_draft_set.v1",
                    "version": "1.0",
                    "status": "draft",
                    "controls": [
                        {
                            "control_id": "loan_disclosure.before_approval",
                            "module_id": "loan_disclosure",
                            "title": "Loan terms are disclosed before approval",
                            "severity": "low",
                            "obligation_ids": [obligation["obligation_id"]],
                            "source_refs": obligation["source_refs"],
                            "applicability_expression": obligation["applicability_expression"],
                            "required_surfaces": ["frontend_h5"],
                            "evidence_requirements": {
                                "frontend_h5": {
                                    "minimum_strength": "static_proof",
                                    "rationale": "Terms must be visible in the user-facing app.",
                                }
                            },
                            "missing_evidence_policy": "block",
                            "reuse_invalidation_keys": ["control_version"],
                        }
                    ],
                }
            )
        )

    phase2 = Phase2CompilationService(
        workspace_root, StaticModelProvider(compilation_response)
    ).compile([policy])
    assert phase2.control_validation.valid
    assert phase2.controls is not None
    assert phase2.controls.controls[0].obligation_ids == ["obl.loan.disclosure"]

    phase3 = phase1_service.compile(run_id="run-e2e")
    assert phase3.coverage is not None
    assert phase3.manifest is not None
    assert len(phase3.work_items) == 1
    assert phase3.work_items[0].collector_fact_refs
    assert phase3.coverage.units[0].work_item_id == phase3.work_items[0].work_item_id
    assert phase3.collector_results

    def review_response(request: ModelRequest) -> ModelResponse:
        if not request.messages or request.messages[-1].get("tool_call_id") is None:
            return ModelResponse(
                tool_calls=[
                    ToolCall(
                        call_id="facts-1",
                        name="get_collector_facts",
                        arguments={"fact_ids": request.work_item.collector_fact_refs[:1]},
                    )
                ]
            )
        fact_message = next(
            message["content"]
            for message in request.messages
            if message.get("tool_call_id") == "facts-1"
        )
        fact_id = request.work_item.collector_fact_refs[0]
        assert fact_id in fact_message
        return ModelResponse(
            content=json.dumps(
                {
                    "contract": "review_result.v1",
                    "work_item_id": request.work_item.work_item_id,
                    "attempt_id": request.attempt_id,
                    "execution_status": "completed",
                    "rows": [
                        {
                            "control_id": request.work_item.control_ids[0],
                            "surface": request.work_item.surface,
                            "evidence_status": "missing",
                            "recommended_control_status": "indeterminate",
                            "gap_reasons": ["fixture runtime result"],
                        }
                    ],
                    "agent_id": request.agent_id,
                }
            )
        )

    run_root = workspace_root / "runs" / "run-e2e"
    summary = LangGraphReviewRuntime(
        provider=StaticModelProvider(review_response), max_concurrency=1
    ).run(
        manifest_run_id=phase3.run_id or "",
        work_items=phase3.work_items,
        sandboxes=phase3.sandboxes,
        output_root=run_root / "reviewer_results",
        event_log_path=run_root / "worker-events.jsonl",
        collector_results=phase3.collector_results,
    )

    assert summary.completed == 1
    assert summary.failed == 0
    assert (
        run_root / "reviewer_results" / phase3.work_items[0].work_item_id / "review-result.json"
    ).is_file()
