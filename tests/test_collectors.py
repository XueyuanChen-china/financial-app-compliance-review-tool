from pathlib import Path

from compliance_review.collectors import (
    ApiDocumentCollector,
    DependencyCollector,
    ManifestCollector,
)
from compliance_review.repository import RepositorySandbox

FIXTURES = Path(__file__).parent / "fixtures" / "day2"


def test_manifest_collector_extracts_permissions_and_components() -> None:
    result = ManifestCollector().collect(
        RepositorySandbox(FIXTURES), "android/AndroidManifest.xml"
    )

    assert result.parser_status == "ok"
    assert result.coverage_status == "complete"
    values = [fact.observed_value for fact in result.facts]
    assert "android.permission.READ_CONTACTS" in values
    assert {value["component"] for value in values if isinstance(value, dict)} >= {
        "activity",
        "service",
    }


def test_manifest_parse_failure_is_structured() -> None:
    result = ManifestCollector().collect(
        RepositorySandbox(FIXTURES), "broken/AndroidManifest.xml"
    )

    assert result.parser_status == "failed"
    assert result.coverage_status == "unknown"
    assert result.facts == []


def test_dependency_collector_extracts_package_dependencies() -> None:
    result = DependencyCollector().collect(
        RepositorySandbox(FIXTURES), input_files=("frontend/package.json",)
    )

    assert result.parser_status == "ok"
    assert result.metadata["dependency_count"] == 3
    assert {fact.observed_value["name"] for fact in result.facts} == {"axios", "vue", "vite"}


def test_dependency_collector_extracts_backend_gradle_dependencies() -> None:
    result = DependencyCollector().collect(
        RepositorySandbox(FIXTURES),
        input_files=("backend/build.gradle.kts",),
        source_surface="backend_code",
    )

    assert result.source_surface == "backend_code"
    assert result.facts[0].observed_value["name"] == "com.example:loan-service"


def test_api_document_collector_extracts_declared_endpoints() -> None:
    result = ApiDocumentCollector().collect(
        RepositorySandbox(FIXTURES),
        roots=("api-doc",),
        file_globs=("*.json",),
    )

    assert result.parser_status == "ok"
    assert result.source_surface == "backend_api_doc"
    assert result.facts[0].evidence_strength == "server_doc"
    endpoints = {fact.observed_value["route"] for fact in result.facts}
    assert endpoints == {"/v1/auth/login", "/v1/loans/{loanId}/repay"}
    operation_ids = {
        fact.observed_value["operation_id"] for fact in result.facts
    }
    assert operation_ids == {"login", "repayLoan", "getRepaymentStatus"}


def test_api_document_collector_reports_fallback_for_broken_document() -> None:
    result = ApiDocumentCollector().collect(
        RepositorySandbox(FIXTURES),
        roots=("api-doc-broken",),
        file_globs=("*.json",),
    )

    assert result.parser_status == "fallback"
    assert result.coverage_status == "unknown"
    assert result.facts == []
