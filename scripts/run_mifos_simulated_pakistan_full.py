"""Run an isolated, full simulated Pakistan lending review for Mifos.

The repositories are real public projects, but the deployment facts in this
script are deliberately hypothetical.  The output therefore keeps a separate
assumptions file and must not be read as evidence that Mifos is a Pakistan
NBFC or that the public repositories belong to a licensed lender.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
from pathlib import Path

from compliance_review.compilation.models import ControlValidationResult
from compliance_review.domain.models import (
    ApplicabilityCondition,
    Control,
    ControlSet,
    EvidenceRequirement,
)
from compliance_review.review.full_review import FullReviewService
from compliance_review.review.langgraph_runtime import LangGraphReviewRuntime
from compliance_review.review.provider import OpenAICompatibleProvider
from compliance_review.setup.service import ReviewSetupService

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BASE_WORKSPACE = (
    PROJECT_ROOT / "test_outputs/mifos-real-e2e-graphify-20260818-fixed-v2"
)
DEFAULT_OUTPUT_ROOT = PROJECT_ROOT / "test_outputs/mifos-simulated-pakistan-full"
DEFAULT_EXTERNAL_MATERIAL_ROOT = (
    PROJECT_ROOT / "test_inputs/external_materials/mifos-pakistan"
)
DEFAULT_BACKEND_API_DOC = PROJECT_ROOT / "test_inputs/backend_api_doc/fineract/fineract.json"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run a full simulated Pakistan lending review on Mifos repositories."
    )
    parser.add_argument("--base-workspace", type=Path, default=DEFAULT_BASE_WORKSPACE)
    parser.add_argument("--workspace", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--run-id", default="mifos-simulated-pakistan-full")
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
    parser.add_argument("--max-concurrency", type=int, default=3)
    parser.add_argument("--token-budget", type=int, default=600_000)
    parser.add_argument(
        "--external-evidence-policy",
        choices=["strict", "trusted_test_materials"],
        default="strict",
        help="Explicitly opt in to trusted external materials for this test run.",
    )
    parser.add_argument(
        "--external-material-root",
        type=Path,
        default=DEFAULT_EXTERNAL_MATERIAL_ROOT,
    )
    parser.add_argument(
        "--backend-api-doc",
        type=Path,
        default=DEFAULT_BACKEND_API_DOC,
        help="Official or explicitly identified OpenAPI/Swagger document to register.",
    )
    parser.add_argument("--force", action="store_true")
    return parser.parse_args()


def _h5_condition() -> ApplicabilityCondition:
    return ApplicabilityCondition(
        kind="atom",
        fact="evidence_surfaces",
        operator="includes",
        value="frontend_h5",
    )


def _scenario_condition(control_id: str) -> ApplicabilityCondition | None:
    conditions = {
        "fin-004": ("short_term_personal_loan", True),
        "fin-005": ("earned_wage_access", True),
        "fin-018": ("targets_thailand", True),
    }
    item = conditions.get(control_id)
    if item is None:
        return None
    fact, value = item
    return ApplicabilityCondition(kind="atom", fact=fact, operator="equals", value=value)


def _add_simulation_conditions(control: Control) -> Control:
    """Make the no-H5 assumption explicit in this isolated scenario only."""
    requirements: dict[str, EvidenceRequirement] = {}
    for surface in control.surface_candidates:
        requirement = control.evidence_requirements.get(surface)
        if requirement is None:
            continue
        if surface != "frontend_h5":
            requirements[surface] = requirement
            continue
        requirements[surface] = requirement.model_copy(update={"condition": _h5_condition()})

    payload = {
        **control.model_dump(mode="json"),
        "candidate_surfaces": list(control.surface_candidates),
        "required_surfaces": list(control.surface_candidates),
        "evidence_requirements": {
            surface: requirement.model_dump(mode="json")
            for surface, requirement in requirements.items()
        },
    }
    scenario_condition = _scenario_condition(control.control_id)
    if scenario_condition is not None:
        payload["applicability_condition"] = scenario_condition.model_dump(mode="json")
    return Control.model_validate(payload)


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def prepare_workspace(
    base_workspace: Path,
    output_workspace: Path,
    force: bool,
    external_evidence_policy: str,
    external_material_root: Path,
    backend_api_doc: Path,
) -> None:
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
    workspace["external_evidence_policy"] = external_evidence_policy
    material_root = external_material_root.expanduser().resolve()
    backend_api_doc = backend_api_doc.expanduser().resolve()
    if not backend_api_doc.is_file():
        raise SystemExit(f"backend API document is missing: {backend_api_doc}")
    external_materials = [
        ("play_console_submission_test.md", "google_play", "play_console"),
        ("secp_nbfc_license_test.md", "country_regulator", "regulator_external"),
        ("external_materials_manifest.json", "internal", "play_console"),
        ("external_materials_manifest.json", "internal", "regulator_external"),
    ]
    for filename, source_family, surface in external_materials:
        material_path = material_root / filename
        if not material_path.is_file():
            raise SystemExit(f"external material is missing: {material_path}")
        workspace.setdefault("materials", []).append(
            {
                "path": material_path.as_posix(),
                "source_family": source_family,
                "surface": surface,
            }
        )
    workspace.setdefault("materials", []).append(
        {
            "path": backend_api_doc.as_posix(),
            "source_family": "backend_api_doc",
            "surface": "backend_api_doc",
            "provenance": {
                "kind": "official_generated_openapi",
                "provider": "Apache Fineract",
                "source_url": "https://sandbox.mifos.community/fineract-provider/fineract.json",
                "retrieved_at": "2026-08-19",
            },
            "limitations": [
                (
                    "Generated by the official Fineract Swagger/OpenAPI pipeline exposed "
                    "by the public sandbox."
                ),
                (
                    "The public sandbox document may differ from the checked-out local "
                    "repository revision."
                ),
                (
                    "Declared API endpoints do not prove runtime authorization, "
                    "persistence, or deployment parity."
                ),
            ],
        }
    )
    _write_json(workspace_path, workspace)

    profile_path = output_workspace / "setup" / "app_profile.json"
    profile = json.loads(profile_path.read_text(encoding="utf-8"))
    fields = profile["fields"]
    fields["app_name"] = {
        "value": "Mifos Mobile Pakistan Lending Demo (simulation)",
        "source": "human_confirmed",
        "confidence": "high",
        "evidence": [],
    }
    fields["jurisdiction"] = {
        "value": "Pakistan",
        "source": "human_confirmed",
        "confidence": "high",
        "evidence": [],
    }
    fields["business_type"] = {
        "value": ["personal_loan", "digital_lending"],
        "source": "human_confirmed",
        "confidence": "high",
        "evidence": [],
    }
    fields["self_lending"] = {
        "value": True,
        "source": "human_confirmed",
        "confidence": "high",
        "evidence": [],
    }
    # These are scenario answers for applicability only. They are not derived
    # from the public repositories and never count as compliance evidence.
    for name, value in {
        "earned_wage_access": False,
        "short_term_personal_loan": False,
        "target_markets": ["Pakistan"],
        "targets_thailand": False,
        "targets_united_states": False,
    }.items():
        fields[name] = {
            "value": value,
            "source": "human_confirmed",
            "confidence": "high",
            "evidence": [],
        }
    fields["evidence_surfaces"]["value"] = [
        "android_native",
        "backend_code",
        "play_console",
        "regulator_external",
    ]
    fields["evidence_surfaces"]["source"] = "human_confirmed"
    fields["evidence_surfaces"]["evidence"] = []
    fields["material_roots"] = {
        "value": {
            "play_console": [
                (material_root / "play_console_submission_test.md").as_posix(),
                (material_root / "external_materials_manifest.json").as_posix(),
            ],
            "regulator_external": [
                (material_root / "secp_nbfc_license_test.md").as_posix(),
                (material_root / "external_materials_manifest.json").as_posix(),
            ],
            "backend_api_doc": [backend_api_doc.as_posix()],
        },
        "source": "deterministic",
        "confidence": "high",
        "evidence": [],
    }
    profile["status"] = "confirmed"
    _write_json(profile_path, profile)

    controls_path = output_workspace / "setup" / "controls.json"
    controls = ControlSet.model_validate(json.loads(controls_path.read_text(encoding="utf-8")))
    simulated_controls = [_add_simulation_conditions(control) for control in controls.controls]
    _write_json(
        controls_path,
        ControlSet(
            contract="control_set.v2",
            version=f"{controls.version}-simulated-pakistan",
            controls=simulated_controls,
        ).model_dump(mode="json"),
    )
    _write_json(
        output_workspace / "setup" / "control_validation.json",
        ControlValidationResult(
            valid=True,
            validated_control_count=len(simulated_controls),
            warnings=[
                "isolated simulation control set; frontend_h5 is conditional on profile presence",
            ],
        ).model_dump(mode="json"),
    )

    assumptions = {
        "simulation_only": True,
        "purpose": (
            "Exercise the full review pipeline with a Pakistan personal-loan "
            "NBFC deployment profile."
        ),
        "real_repositories": {
            "android_native": fields["repository_roots"]["value"]["android_native"],
            "backend_code": fields["repository_roots"]["value"]["backend_code"],
        },
        "assumed_deployment_facts": [
            "The deployment targets Pakistan.",
            "The deployment offers personal loans/digital lending.",
            "The deployment is treated as self-lending for applicability simulation.",
            "The deployment has no frontend_h5/WebView surface.",
            "The deployment does not offer Earned Wage Access (EWA).",
            "The deployment does not promote loans repayable in 60 days or less.",
            "The deployment does not target Thailand or the United States.",
        ],
        "external_evidence_policy": external_evidence_policy,
        "external_material_manifest": (
            "The run reads the registered external_materials_manifest.json and the "
            "two referenced material files without changing their contents."
        ),
        "interpretation_rule": (
            "Simulation assumptions may reduce applicability unknowns but never "
            "become static code evidence."
        ),
    }
    _write_json(output_workspace / "setup" / "simulation_assumptions.json", assumptions)
    (output_workspace / "setup" / "simulation_assumptions.md").write_text(
        "# 模拟场景假设\n\n"
        "> 本文件只用于测试流程，不证明 Mifos Mobile 或 Fineract 属于巴基斯坦持牌 NBFC。\n\n"
        "- 司法辖区：Pakistan\n"
        "- 业务类型：personal_loan / digital_lending\n"
        "- 自营放贷：假设为 true\n"
        "- H5/WebView：确认不存在，因此 frontend_h5 只作为条件候选面\n"
        f"- 外部材料策略：{external_evidence_policy}\n"
        "- Play Console 与 SECP 材料：按 workspace 中登记的材料和 manifest 读取\n\n"
        "代码仓库证据仍只来自真实的 Android 与 backend_code 仓库；"
        "外部材料会继续显示为人工/外部证据要求。\n",
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


def main() -> None:
    args = parse_args()
    prepare_workspace(
        args.base_workspace,
        args.workspace,
        args.force,
        args.external_evidence_policy,
        args.external_material_root,
        args.backend_api_doc,
    )
    workspace = args.workspace.expanduser().resolve()
    provider = OpenAICompatibleProvider(
        model=args.model,
        base_url=args.base_url,
        timeout_seconds=180.0,
    )
    setup = ReviewSetupService(workspace, applicability_provider=provider).compile(
        run_id=args.run_id,
        mode="full",
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
    applicability = setup.applicability
    print(
        json.dumps(
            {
                "workspace": workspace.as_posix(),
                "run_id": result.snapshot.run_id,
                "simulation_assumptions": (
                    workspace / "setup" / "simulation_assumptions.md"
                ).as_posix(),
                "control_count": len(controls.controls),
                "coverage_units": len(setup.coverage.units if setup.coverage else []),
                "work_items": len(setup.work_items),
                "work_item_ids": [item.work_item_id for item in setup.work_items],
                "applicability_counts": {
                    "applicable": sum(
                        decision.decision == "applicable"
                        for decision in (applicability.decisions if applicability else [])
                    ),
                    "not_applicable": sum(
                        decision.decision == "not_applicable"
                        for decision in (applicability.decisions if applicability else [])
                    ),
                    "unknown": sum(
                        decision.decision == "unknown"
                        for decision in (applicability.decisions if applicability else [])
                    ),
                },
                "ci_status": result.snapshot.ci_status,
                "report": (workspace / "runs" / result.snapshot.run_id / "report.md").as_posix(),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
