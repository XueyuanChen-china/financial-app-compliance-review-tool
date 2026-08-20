"""Run one Control through an isolated end-to-end diagnostic review.

The harness copies repository and policy inputs from a prepared fixture, but
rebuilds the workspace, AppProfile, applicability state, Coverage, and review
run from scratch. It keeps the Control Set intentionally small for fast tests
without reusing stale setup conclusions.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
from pathlib import Path
from typing import Literal

from pydantic import TypeAdapter

from compliance_review.compilation.models import ControlValidationResult
from compliance_review.domain.models import (
    ApplicabilityCondition,
    Control,
    ControlSet,
    EvidenceRequirement,
    Surface,
)
from compliance_review.review.full_review import FullReviewService
from compliance_review.review.langgraph_runtime import LangGraphReviewRuntime
from compliance_review.review.provider import OpenAICompatibleProvider
from compliance_review.setup.models import ComplianceWorkspace
from compliance_review.setup.service import ReviewSetupService

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BASE_WORKSPACE = (
    PROJECT_ROOT / "test_outputs/mifos-real-e2e-graphify-20260818-fixed-v2"
)
DEFAULT_OUTPUT_ROOT = PROJECT_ROOT / "test_outputs/single-control-review"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run one compliance Control end to end.")
    parser.add_argument("--control-id", default="fin-001")
    parser.add_argument("--base-workspace", type=Path, default=DEFAULT_BASE_WORKSPACE)
    parser.add_argument("--workspace", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--run-id", default="single-fin-001")
    parser.add_argument(
        "--model", default=os.environ.get("COMPLIANCE_REVIEW_MODEL", "gpt-5.6-luna")
    )
    parser.add_argument(
        "--base-url",
        default=os.environ.get(
            "COMPLIANCE_REVIEW_BASE_URL",
            "http://8.137.80.16:8083/v1/chat/completions",
        ),
    )
    parser.add_argument("--max-concurrency", type=int, default=1)
    parser.add_argument("--token-budget", type=int, default=64000)
    parser.add_argument("--test-jurisdiction", default="Pakistan")
    parser.add_argument(
        "--test-business-type",
        action="append",
        default=None,
        help="Test-only business type; repeat for multiple values",
    )
    parser.add_argument(
        "--test-self-lending",
        action=argparse.BooleanOptionalAction,
        default=True,
    )
    parser.add_argument(
        "--material-surface",
        action="append",
        default=[],
        help="Register an evidence material for this test as path=surface",
    )
    parser.add_argument("--force", action="store_true")
    return parser.parse_args()


def _condition(
    fact: str, operator: Literal["equals", "includes"], value: object
) -> ApplicabilityCondition:
    return ApplicabilityCondition(
        kind="atom", fact=fact, operator=operator, value=value
    )


def _fin001_candidate_control(
    control: Control,
    include_backend_api_doc: bool = False,
) -> Control:
    """Apply the policy-specific candidate surface fixture for the fast test."""
    requirements = {
        "android_native": EvidenceRequirement(
            minimum_strength="static_proof",
            rationale="Inspect the native user-facing financial disclosure path.",
            obligation_ids=control.obligation_ids,
            source_refs=control.source_refs,
        ),
        "frontend_h5": EvidenceRequirement(
            minimum_strength="static_proof",
            rationale="Inspect H5 disclosure only when the AppProfile confirms an H5 surface.",
            obligation_ids=control.obligation_ids,
            source_refs=control.source_refs,
            condition=_condition("evidence_surfaces", "includes", "frontend_h5"),
        ),
        "play_console": EvidenceRequirement(
            minimum_strength="declared",
            rationale="Manual verification of financial-services listing and declarations.",
            obligation_ids=control.obligation_ids,
            source_refs=control.source_refs,
        ),
        "regulator_external": EvidenceRequirement(
            minimum_strength="declared",
            rationale="Manual verification of the target-country regulatory basis.",
            obligation_ids=control.obligation_ids,
            source_refs=control.source_refs,
        ),
    }
    if include_backend_api_doc:
        requirements["backend_api_doc"] = EvidenceRequirement(
            minimum_strength="server_doc",
            rationale=(
                "Inspect the registered derived API inventory as a declared API "
                "surface; it does not prove runtime reachability or authorization."
            ),
            obligation_ids=control.obligation_ids,
            source_refs=control.source_refs,
        )
    return Control.model_validate(
        {
            **control.model_dump(mode="json"),
            "candidate_surfaces": list(requirements),
            "required_surfaces": list(requirements),
            "evidence_requirements": {
                surface: requirement.model_dump(mode="json")
                for surface, requirement in requirements.items()
            },
            "minimum_evidence_strength": {
                surface: requirement.minimum_strength
                for surface, requirement in requirements.items()
            },
        }
    )


def prepare_workspace(
    base_workspace: Path,
    output_workspace: Path,
    control_id: str,
    material_surface: list[str],
    force: bool,
) -> ComplianceWorkspace:
    base_workspace = base_workspace.expanduser().resolve()
    output_workspace = output_workspace.expanduser().resolve()
    if not (base_workspace / "workspace.json").is_file():
        raise SystemExit(f"base workspace is missing workspace.json: {base_workspace}")
    if output_workspace.exists():
        if not force:
            raise SystemExit(f"output workspace exists; use --force: {output_workspace}")
        shutil.rmtree(output_workspace)
    shutil.copytree(base_workspace, output_workspace)

    workspace_path = output_workspace / "workspace.json"
    workspace = json.loads(workspace_path.read_text(encoding="utf-8"))
    workspace["workspace_root"] = output_workspace.as_posix()
    for item in material_surface:
        if "=" not in item:
            raise SystemExit("--material-surface must use path=surface")
        raw_path, surface = item.split("=", 1)
        path = Path(raw_path).expanduser().resolve()
        if not path.exists():
            raise SystemExit(f"material path does not exist: {path}")
        try:
            parsed_surface: Surface = TypeAdapter(Surface).validate_python(surface)
        except ValueError as exc:
            raise SystemExit(f"invalid material surface: {surface}") from exc
        source_family = {
            "play_console": "google_play",
            "regulator_external": "country_regulator",
            "backend_api_doc": "backend_api_doc",
        }.get(parsed_surface, parsed_surface)
        workspace.setdefault("materials", []).append(
            {
                "path": path.as_posix(),
                "source_family": source_family,
                "surface": parsed_surface,
            }
        )
    workspace_path.write_text(json.dumps(workspace, ensure_ascii=False, indent=2) + "\n")
    workspace_model = ComplianceWorkspace.model_validate(workspace)

    controls_path = output_workspace / "setup" / "controls.json"
    controls = ControlSet.model_validate(json.loads(controls_path.read_text(encoding="utf-8")))
    selected = [control for control in controls.controls if control.control_id == control_id]
    if len(selected) != 1:
        raise SystemExit(
            f"expected exactly one Control {control_id!r}, found {len(selected)}"
        )
    control = selected[0]
    if control_id == "fin-001":
        control = _fin001_candidate_control(
            control,
            include_backend_api_doc=any(
                item.rsplit("=", 1)[-1] == "backend_api_doc" for item in material_surface
            ),
        )
    selected_set = ControlSet(
        contract="control_set.v2",
        version=f"{controls.version}-single-{control_id}",
        controls=[control],
    )
    controls_path.write_text(
        json.dumps(selected_set.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    validation = ControlValidationResult(
        valid=True,
        validated_control_count=1,
        warnings=["single-control diagnostic workspace; not a full Control Set"],
    )
    (output_workspace / "setup" / "control_validation.json").write_text(
        json.dumps(validation.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    for name in (
        "applicability.json",
        "applicability_resolution.json",
        "applicability_resolution_checkpoint.json",
        "applicability_answers.json",
        "coverage_units.json",
    ):
        (output_workspace / "setup" / name).unlink(missing_ok=True)
    shutil.rmtree(output_workspace / "runs", ignore_errors=True)
    return workspace_model


def main() -> None:
    args = parse_args()
    prepare_workspace(
        args.base_workspace,
        args.workspace,
        args.control_id,
        args.material_surface,
        args.force,
    )
    workspace = args.workspace.expanduser().resolve()
    provider = OpenAICompatibleProvider(
        model=args.model,
        base_url=args.base_url,
        timeout_seconds=180.0,
    )
    workspace_model = ComplianceWorkspace.model_validate(
        json.loads((workspace / "workspace.json").read_text(encoding="utf-8"))
    )
    setup_service = ReviewSetupService(workspace, applicability_provider=provider)
    setup_service.initialize(workspace_model.repositories, workspace_model.materials)
    setup_service.confirm_profile(
        {
            "app_name": "Mifos Mobile Pakistan test fixture",
            "package_name": "org.mifos.mobile",
            "jurisdiction": args.test_jurisdiction,
            "business_type": args.test_business_type or ["personal_loan"],
            "self_lending": args.test_self_lending,
            "distribution_channels": ["google_play"],
        }
    )
    setup = setup_service.compile(
        run_id=args.run_id,
        max_concurrency=args.max_concurrency,
    )
    controls = ControlSet.model_validate(
        json.loads((workspace / "setup" / "controls.json").read_text(encoding="utf-8"))
    )
    result = FullReviewService(
        workspace,
        LangGraphReviewRuntime(
            provider=provider,
            max_concurrency=args.max_concurrency,
            token_budget=args.token_budget,
        ),
    ).run(setup, controls)
    applicability = setup.applicability.decisions[0] if setup.applicability else None
    print(
        json.dumps(
            {
                "workspace": workspace.as_posix(),
                "control_id": args.control_id,
                "coverage_units": len(setup.coverage.units if setup.coverage else []),
                "resolved_required_surfaces": [
                    *(applicability.resolved_required_surfaces if applicability else [])
                ],
                "work_items": len(setup.work_items),
                "work_item_ids": [item.work_item_id for item in setup.work_items],
                "ci_status": result.snapshot.ci_status,
                "report": (workspace / "runs" / result.snapshot.run_id / "report.md").as_posix(),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
