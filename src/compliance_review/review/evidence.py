from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Literal

from compliance_review.domain.models import EvidenceAnchor, EvidenceStrength, Surface

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


def canonical_anchor_identity(anchor: EvidenceAnchor) -> tuple[object, ...]:
    """Return the Control-neutral identity used by every evidence boundary."""

    return (
        anchor.repository_id,
        anchor.source_surface,
        anchor.path,
        anchor.start_line,
        anchor.end_line,
        anchor.normalized_snippet_hash,
        anchor.file_revision,
    )


def canonical_anchor_id(
    *,
    repository_id: str,
    source_surface: Surface,
    path: str | None,
    start_line: int | None,
    end_line: int | None,
    normalized_snippet_hash: str | None,
    file_revision: str | None,
) -> str:
    """Build a stable ID without tool-call or Control ownership fields."""

    payload = "|".join(
        str(value)
        for value in (
            repository_id,
            source_surface,
            path,
            start_line,
            end_line,
            normalized_snippet_hash,
            file_revision,
        )
    )
    return "anchor." + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:20]


def build_verified_anchor(
    *,
    repository_id: str,
    source_surface: Surface,
    source_tool: str,
    path: str,
    start_line: int,
    end_line: int,
    exact_snippet: str,
    file_revision: str,
    evidence_strength: EvidenceStrength,
    symbol: str | None = None,
    fact_ids: list[str] | None = None,
    summary: str = "Programmatically verified source range.",
) -> EvidenceAnchor:
    """Create the only accepted shape for a verified source Anchor."""

    if start_line < 1 or end_line < start_line:
        raise ValueError("verified anchor line range is invalid")
    if not exact_snippet.strip() or is_generic_anchor_snippet(exact_snippet):
        raise ValueError("verified anchor requires a specific source snippet")
    if not file_revision.strip():
        raise ValueError("verified anchor requires a file revision")
    normalized_hash = hashlib.sha256(
        normalize_snippet(exact_snippet).encode("utf-8")
    ).hexdigest()
    return EvidenceAnchor(
        anchor_id=canonical_anchor_id(
            repository_id=repository_id,
            source_surface=source_surface,
            path=path,
            start_line=start_line,
            end_line=end_line,
            normalized_snippet_hash=normalized_hash,
            file_revision=file_revision,
        ),
        repository_id=repository_id,
        source_surface=source_surface,
        source_tool=source_tool,
        path=path,
        symbol=symbol,
        start_line=start_line,
        end_line=end_line,
        exact_snippet=exact_snippet,
        normalized_snippet_hash=normalized_hash,
        file_revision=file_revision,
        evidence_strength=evidence_strength,
        fact_ids=sorted(set(fact_ids or [])),
        summary=summary,
    )


def is_generic_anchor_snippet(value: str) -> bool:
    """Reject tag prefixes that identify a class of lines, not one fact."""

    normalized = normalize_snippet(value).lower()
    return bool(
        normalized in {"<uses-permission", "<uses-sdk", "<permission"}
        or re.fullmatch(r"</?[a-z][a-z0-9_.:-]*", normalized) is not None
    )


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
    if anchor.start_line is not None and anchor.end_line is not None:
        declared = "\n".join(lines[anchor.start_line - 1 : anchor.end_line])
        if normalize_snippet(declared) == normalized:
            direct_status: Literal["unchanged", "relocated"] = (
                "unchanged" if old_revision == current_revision else "relocated"
            )
            return AnchorRelocationResult(
                anchor_id,
                direct_status,
                old_start_line=anchor.start_line,
                old_end_line=anchor.end_line,
                new_start_line=anchor.start_line,
                new_end_line=anchor.end_line,
                reason="declared location matches current source",
            )
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
