from __future__ import annotations

import json
from pathlib import Path

import pytest

from compliance_review.persistence import ArtifactStore, WorkspacePathViolation
from compliance_review.repository import RepositorySandbox
from compliance_review.review.models import ModelResponse, ToolCall
from compliance_review.review.provider import StaticModelProvider
from compliance_review.setup.app_facts import collect_app_facts
from compliance_review.setup.models import WorkspaceRepository
from compliance_review.setup.profile import ProfileAgent
from compliance_review.setup.repository_inventory import build_repository_inventory
from compliance_review.setup.service import ReviewSetupService

FIXTURES = Path(__file__).parent / "fixtures" / "day2"


def test_inventory_detects_declared_android_surface() -> None:
    inventory = build_repository_inventory(
        WorkspaceRepository(
            repo_id="mobile",
            path=(FIXTURES / "android").as_posix(),
            declared_surface="android_native",
        )
    )

    assert inventory.surface_status == "confirmed"
    assert inventory.detected_surface == "android_native"
    assert inventory.is_git_repository is False
    assert any(signal.signal_type == "android_manifest" for signal in inventory.detection_signals)


def test_inventory_marks_declared_surface_conflict_unresolved() -> None:
    inventory = build_repository_inventory(
        WorkspaceRepository(
            repo_id="web",
            path=(FIXTURES / "frontend").as_posix(),
            declared_surface="android_native",
        )
    )

    assert inventory.surface_status == "unresolved"
    assert inventory.detected_surface is None
    assert inventory.detected_surface != inventory.declared_surface


def test_app_facts_reuse_collectors_without_llm() -> None:
    inventory = build_repository_inventory(
        WorkspaceRepository(repo_id="android", path=(FIXTURES / "android").as_posix())
    )
    facts = collect_app_facts([inventory])

    assert facts.contract == "app_fact_set.v1"
    assert any(fact.fact_type == "android_manifest_permission" for fact in facts.facts)
    assert any(result["collector_id"] == "android_manifest" for result in facts.collector_results)


def test_setup_service_persists_conservative_profile_draft(tmp_path: Path) -> None:
    result = ReviewSetupService(tmp_path / "workspace").initialize(
        [WorkspaceRepository(repo_id="web", path=(FIXTURES / "frontend").as_posix())]
    )

    assert result.profile.status == "awaiting_confirmation"
    assert result.profile.value_for("self_lending") == "unknown"
    assert result.confirmation.status == "awaiting_confirmation"
    assert "jurisdiction" in result.confirmation.required_fields
    assert (tmp_path / "workspace" / "workspace.json").is_file()
    profile = json.loads(
        (tmp_path / "workspace" / "setup" / "app_profile_draft.json").read_text()
    )
    assert profile["fields"]["evidence_surfaces"]["value"] == ["frontend_h5"]
    assert not (tmp_path / "workspace" / "setup" / "app_profile.json").exists()


def test_artifact_store_rejects_workspace_escape(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "workspace")

    with pytest.raises(WorkspacePathViolation):
        store._write_json("../outside.json", {"secret": True})


def test_profile_confirmation_writes_only_confirmed_profile(tmp_path: Path) -> None:
    service = ReviewSetupService(tmp_path / "workspace")
    service.initialize(
        [WorkspaceRepository(repo_id="web", path=(FIXTURES / "frontend").as_posix())]
    )

    confirmed = service.confirm_profile(
        {
            "app_name": "Example Loan",
            "package_name": "com.example.loan",
            "jurisdiction": "Pakistan",
            "business_type": ["personal_loan"],
            "self_lending": True,
        }
    )

    assert confirmed.status == "confirmed"
    assert (tmp_path / "workspace" / "setup" / "app_profile.json").is_file()
    confirmation = json.loads(
        (tmp_path / "workspace" / "setup" / "app_profile_confirmation.json").read_text()
    )
    assert confirmation["status"] == "confirmed"


def test_profile_agent_returns_structured_object_without_write_tools() -> None:
    inventory = build_repository_inventory(
        WorkspaceRepository(repo_id="web", path=(FIXTURES / "frontend").as_posix())
    )
    facts = collect_app_facts([inventory])
    response = {
        "contract": "app_profile.v1",
        "version": "1.0",
        "status": "draft",
        "fields": {
            "app_name": {
                "value": "Example",
                "source": "inferred",
                "confidence": "medium",
                "evidence": [],
            }
        },
    }
    provider = StaticModelProvider(lambda request: ModelResponse(content=json.dumps(response)))
    profile = ProfileAgent(provider).run(
        inventory,
        facts,
        RepositorySandbox(Path(inventory.path)),
    )

    request = provider.requests[0]
    tool_names = {schema["function"]["name"] for schema in request.tools}
    assert profile.status == "draft"
    assert "write_file" not in tool_names
    assert "get_repository_inventory" in tool_names
    assert "get_app_facts" in tool_names


def test_profile_agent_runs_model_tool_loop_inside_langgraph_subgraph() -> None:
    inventory = build_repository_inventory(
        WorkspaceRepository(repo_id="web", path=(FIXTURES / "frontend").as_posix())
    )
    facts = collect_app_facts([inventory])
    response = {
        "contract": "app_profile.v1",
        "version": "1.0",
        "status": "draft",
        "fields": {
            "app_name": {
                "value": "Example",
                "source": "inferred",
                "confidence": "medium",
                "evidence": [],
            }
        },
    }
    calls = iter(
        [
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        call_id="profile-tool-1",
                        name="get_app_facts",
                        arguments={},
                    )
                ]
            ),
            ModelResponse(content=json.dumps(response)),
        ]
    )
    provider = StaticModelProvider(lambda request: next(calls))

    profile = ProfileAgent(provider, max_rounds=2).run(
        inventory,
        facts,
        RepositorySandbox(Path(inventory.path)),
    )

    assert profile.status == "draft"
    assert len(provider.requests) == 2
    assert provider.requests[1].messages[-1]["tool_call_id"] == "profile-tool-1"
