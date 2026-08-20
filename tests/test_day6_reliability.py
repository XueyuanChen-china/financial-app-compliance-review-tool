from __future__ import annotations

import hashlib
import time
from pathlib import Path

import pytest

from compliance_review.domain.models import EvidenceAnchor, WorkItem
from compliance_review.repository import RepositorySandbox
from compliance_review.review.events import AppendOnlyEventLog
from compliance_review.review.evidence import (
    file_content_revision,
    relocate_anchor,
)
from compliance_review.review.models import ToolCall
from compliance_review.review.redaction import redact_sensitive_text
from compliance_review.review.reliability import (
    ModelTimeoutError,
    call_with_timeout,
    classify_error,
)
from compliance_review.review.tools import ScopedToolExecutor


def _anchor(snippet: str, start_line: int = 1) -> EvidenceAnchor:
    return EvidenceAnchor(
        anchor_id="anchor.test",
        control_ids=["control.test"],
        source_surface="frontend_h5",
        source_tool="read_file",
        path="src/app.js",
        start_line=start_line,
        end_line=start_line,
        exact_snippet=snippet,
        normalized_snippet_hash=hashlib.sha256(snippet.encode()).hexdigest(),
        evidence_strength="static_proof",
        summary="fixture",
    )


def test_unique_anchor_relocation_succeeds() -> None:
    anchor = _anchor("const consent = true;", start_line=1)
    current = "const mode = 'new';\nconst consent = true;\n"

    result = relocate_anchor(
        anchor,
        current,
        file_content_revision(current.encode()),
        anchor.file_revision,
    )

    assert result.status == "relocated"
    assert result.new_start_line == 2


def test_exact_declared_location_wins_over_duplicate_snippet() -> None:
    anchor = _anchor("const consent = true;")
    current = "const consent = true;\nconst other = 1;\nconst consent = true;\n"

    result = relocate_anchor(
        anchor,
        current,
        file_content_revision(current.encode()),
        anchor.file_revision,
    )

    assert result.status == "relocated"
    assert result.new_start_line == 1
    assert result.new_end_line == 1


def test_missing_anchor_relocation_is_rejected() -> None:
    anchor = _anchor("const consent = true;")
    current = "const consent = false;\n"

    result = relocate_anchor(
        anchor,
        current,
        file_content_revision(current.encode()),
        anchor.file_revision,
    )

    assert result.status == "missing"


def test_redaction_covers_bearer_assignments_and_pem() -> None:
    text = (
        "Authorization: Bearer abc123 OPENAI_API_KEY=secret123\n"
        "-----BEGIN PRIVATE KEY-----\nprivate\n-----END PRIVATE KEY-----"
    )

    redacted = redact_sensitive_text(text)

    assert "abc123" not in redacted
    assert "secret123" not in redacted
    assert "private" not in redacted
    assert redacted.count("[REDACTED]") == 3


def test_repository_sandbox_rejects_symlink_escape(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (outside / "secret.txt").write_text("secret", encoding="utf-8")
    (root / "link").symlink_to(outside, target_is_directory=True)
    sandbox = RepositorySandbox(root)

    with pytest.raises(ValueError, match="leaves repository root"):
        sandbox.read_text("link/secret.txt")


def test_repository_sandbox_rejects_sibling_prefix_escape(tmp_path: Path) -> None:
    root = tmp_path / "app"
    sibling = tmp_path / "application"
    root.mkdir()
    sibling.mkdir()
    (sibling / "secret.txt").write_text("secret", encoding="utf-8")
    sandbox = RepositorySandbox(root)

    with pytest.raises(ValueError, match="leaves repository root"):
        sandbox.read_text("../application/secret.txt")


def test_path_escape_is_non_retryable() -> None:
    classification = classify_error(ValueError("path escape security policy violation"))

    assert classification.error_code == "path_escape"
    assert classification.retryable is False


def test_model_timeout_has_explicit_error_code() -> None:
    with pytest.raises(ModelTimeoutError) as error:
        call_with_timeout(lambda: time.sleep(0.05), 0.001, ModelTimeoutError("timed out"))

    assert error.value.error_code == "model_timeout"


def test_tool_and_event_outputs_redact_secrets_without_mutating_source(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    source = "Authorization: Bearer live-token\nOPENAI_API_KEY=api-secret\n"
    source_path = root / "src" / "config.js"
    source_path.write_text(source, encoding="utf-8")
    work_item = WorkItem(
        work_item_id="wi.redaction",
        module_id="privacy",
        surface="frontend_h5",
        control_ids=["privacy.test"],
        allowed_roots=["src"],
    )
    result = ScopedToolExecutor(RepositorySandbox(root), work_item).execute(
        ToolCall(
            call_id="read-secret",
            name="read_file",
            arguments={"path": "src/config.js"},
        )
    )
    event_path = tmp_path / "events.jsonl"
    AppendOnlyEventLog(event_path).append(
        "tool_result",
        {"output": result.output, "error": "password=error-secret"},
    )

    serialized = str(result.output)
    logged = event_path.read_text(encoding="utf-8")
    assert "live-token" not in serialized
    assert "api-secret" not in serialized
    assert "error-secret" not in logged
    assert source_path.read_text(encoding="utf-8") == source
