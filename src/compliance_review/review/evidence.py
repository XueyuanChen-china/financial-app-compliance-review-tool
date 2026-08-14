from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Literal

from compliance_review.domain.models import EvidenceAnchor, EvidenceStrength

EVIDENCE_STRENGTH_RANK: dict[EvidenceStrength, int] = {
    "declared": 0,
    "behavioral_hint": 1,
    "static_proof": 2,
    "server_doc": 3,
    "server_code": 4,
    "runtime_proof": 5,
}


def normalize_snippet(value: str) -> str:
    return " ".join(value.split())


def file_content_revision(content: bytes) -> str:
    header = f"blob {len(content)}\0".encode()
    return f"git-blob-sha1:{hashlib.sha1(header + content).hexdigest()}"


def strongest_evidence_strength(
    values: list[EvidenceStrength],
) -> EvidenceStrength | None:
    if not values:
        return None
    return max(values, key=EVIDENCE_STRENGTH_RANK.__getitem__)


@dataclass(frozen=True)
class AnchorRelocationResult:
    anchor_id: str
    status: Literal["unchanged", "relocated", "missing", "ambiguous"]
    old_start_line: int | None = None
    old_end_line: int | None = None
    new_start_line: int | None = None
    new_end_line: int | None = None
    reason: str = ""


def relocate_anchor(
    anchor: EvidenceAnchor,
    text: str,
    current_revision: str,
    old_revision: str | None,
) -> AnchorRelocationResult:
    """Find a unique normalized snippet location without using an LLM."""
    anchor_id = anchor.anchor_id
    exact_snippet = anchor.exact_snippet
    if not isinstance(exact_snippet, str) or not exact_snippet.strip():
        return AnchorRelocationResult(anchor_id, "missing", reason="anchor has no exact snippet")
    normalized = normalize_snippet(exact_snippet)
    lines = text.splitlines()
    matches: list[tuple[int, int]] = []
    for start in range(len(lines)):
        for end in range(start + 1, len(lines) + 1):
            candidate = normalize_snippet("\n".join(lines[start:end]))
            if candidate == normalized:
                matches.append((start + 1, end))
            if len(candidate) > len(normalized):
                break
    if not matches:
        return AnchorRelocationResult(
            anchor_id,
            "missing",
            old_start_line=anchor.start_line,
            old_end_line=anchor.end_line,
            reason="normalized snippet is absent",
        )
    if len(matches) > 1:
        return AnchorRelocationResult(
            anchor_id,
            "ambiguous",
            old_start_line=anchor.start_line,
            old_end_line=anchor.end_line,
            reason="normalized snippet has multiple matches",
        )
    new_start, new_end = matches[0]
    old_start = anchor.start_line
    old_end = anchor.end_line or old_start
    status: Literal["unchanged", "relocated"] = (
        "unchanged"
        if old_start == new_start and old_end == new_end and old_revision == current_revision
        else "relocated"
    )
    return AnchorRelocationResult(
        anchor_id,
        status,
        old_start_line=old_start,
        old_end_line=old_end,
        new_start_line=new_start,
        new_end_line=new_end,
        reason="unique normalized snippet match",
    )
