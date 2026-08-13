from __future__ import annotations

import hashlib

from compliance_review.domain.models import EvidenceStrength

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
