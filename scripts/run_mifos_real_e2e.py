"""Run the real public Mifos Mobile + Fineract Google Play E2E scenario.

This is a local demonstration script, not the end-user interface. It models the
answers a user would confirm after deterministic repository discovery creates a
draft. A large-repository Profile Agent pass is intentionally not a prerequisite
for this E2E run; it can be tested separately with tighter discovery budgets.

The scenario intentionally does not claim that Mifos is a Pakistan NBFC or a
direct personal-loan lender. It uses only Google Play source material and keeps
the jurisdiction/business facts outside that claim conservative.
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from compliance_review.compilation.service import Phase2CompilationService
from compliance_review.review.full_review import FullReviewService
from compliance_review.review.langgraph_runtime import LangGraphReviewRuntime
from compliance_review.review.provider import OpenAICompatibleProvider
from compliance_review.setup.models import WorkspaceMaterial, WorkspaceRepository
from compliance_review.setup.service import ReviewSetupService

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ANDROID_ROOT = Path.home() / "Desktop/test-projects/mifos-mobile"
DEFAULT_BACKEND_ROOT = Path.home() / "Desktop/test-projects/fineract"
DEFAULT_POLICY_SOURCES = [
    PROJECT_ROOT / "test_inputs/policy_sources/google_play/financial_services.txt",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the real Mifos Mobile + Fineract Google Play E2E review."
    )
    parser.add_argument("--android-root", type=Path, default=DEFAULT_ANDROID_ROOT)
    parser.add_argument("--backend-root", type=Path, default=DEFAULT_BACKEND_ROOT)
    parser.add_argument(
        "--policy-source",
        action="append",
        dest="policy_sources",
        type=Path,
        help="Relevant raw policy file; repeat for additional sources.",
    )
    parser.add_argument(
        "--workspace",
        type=Path,
        default=PROJECT_ROOT / "test_outputs/mifos-real-e2e",
    )
    parser.add_argument("--run-id", default=None)
    parser.add_argument(
        "--model",
        default=os.environ.get("COMPLIANCE_REVIEW_MODEL", "gpt-5.6-luna"),
    )
    parser.add_argument(
        "--base-url",
        default=os.environ.get(
            "COMPLIANCE_REVIEW_BASE_URL",
            "http://8.137.80.16:8083/v1/chat/completions",
        ),
    )
    parser.add_argument("--max-concurrency", type=int, default=2)
    parser.add_argument("--token-budget", type=int, default=600_000)
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=float(os.environ.get("COMPLIANCE_REVIEW_TIMEOUT_SECONDS", "180")),
    )
    return parser.parse_args()


def require_directory(path: Path, label: str) -> Path:
    resolved = path.expanduser().resolve()
    if not resolved.is_dir():
        raise SystemExit(f"{label} directory does not exist: {resolved}")
    return resolved


def main() -> None:
    args = parse_args()
    android_root = require_directory(args.android_root, "Android repository")
    backend_root = require_directory(args.backend_root, "backend repository")
    policy_sources = [
        path.expanduser().resolve()
        for path in (args.policy_sources or DEFAULT_POLICY_SOURCES)
    ]
    for policy_source in policy_sources:
        if not policy_source.is_file():
            raise SystemExit(f"Google Play policy file does not exist: {policy_source}")
    workspace = args.workspace.expanduser().resolve()
    run_id = args.run_id or "mifos-real-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    provider = OpenAICompatibleProvider(
        model=args.model,
        base_url=args.base_url,
        timeout_seconds=args.timeout_seconds,
    )

    setup_service = ReviewSetupService(
        workspace,
        applicability_provider=provider,
    )
    setup_service.initialize(
        [
            WorkspaceRepository(
                repo_id="mifos_mobile",
                path=android_root.as_posix(),
                declared_surface="android_native",
            ),
            WorkspaceRepository(
                repo_id="fineract",
                path=backend_root.as_posix(),
                declared_surface="backend_code",
            ),
        ],
        materials=[
            WorkspaceMaterial(path=path.as_posix(), source_family="google_play")
            for path in policy_sources
        ],
    )

    # These are explicit scenario confirmations, not facts inferred from source
    # code. Unknown public-project context must not become a Pakistan NBFC claim.
    setup_service.confirm_profile(
        {
            "app_name": "Mifos Mobile",
            "package_name": "org.mifos.mobile",
            "jurisdiction": "not_country_specific_public_project",
            "business_type": ["banking", "microfinance"],
            "self_lending": False,
        },
        repository_surfaces={
            "mifos_mobile": "android_native",
            "fineract": "backend_code",
        },
    )

    compilation = Phase2CompilationService(workspace, provider).compile(
        policy_sources,
        source_families={path.as_posix(): "google_play" for path in policy_sources},
    )
    setup = setup_service.compile(
        run_id=run_id,
        mode="full",
        max_concurrency=args.max_concurrency,
    )
    result = FullReviewService(
        workspace,
        LangGraphReviewRuntime(
            provider=provider,
            max_concurrency=args.max_concurrency,
            token_budget=args.token_budget,
        ),
    ).run(setup, compilation.controls)

    payload = {
        "workspace": workspace.as_posix(),
        "run_id": result.snapshot.run_id,
        "policy_family": "google_play",
        "source_count": len(compilation.source_registry.sources),
        "obligation_count": len(compilation.obligations.obligations),
        "control_count": len(compilation.controls.controls),
        "coverage_unit_count": len(setup.coverage.units if setup.coverage else []),
        "work_item_count": len(setup.work_items),
        "work_item_ids": [item.work_item_id for item in setup.work_items],
        "ci_status": result.snapshot.ci_status,
        "report": (workspace / "runs" / result.snapshot.run_id / "report.md").as_posix(),
        "profile": (workspace / "setup" / "app_profile.json").as_posix(),
        "controls": (workspace / "setup" / "controls.json").as_posix(),
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
