from __future__ import annotations

from typing import Any, Optional, Tuple

from pydantic import BaseModel, ConfigDict, Field

from compliance_review.domain.models import CoverageStatus, Fact, ParserStatus, Surface


class CollectorResult(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    collector_id: str = Field(min_length=1)
    repo_id: Optional[str] = Field(default=None, min_length=1)
    source_surface: Surface
    parser_status: ParserStatus
    coverage_status: CoverageStatus
    input_files: list[str] = Field(default_factory=list)
    facts: list[Fact] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


def status_for_inputs(
    input_files: list[str], parse_failures: int
) -> Tuple[ParserStatus, CoverageStatus]:
    if not input_files:
        return "failed", "unknown"
    if parse_failures:
        return "fallback", "partial"
    return "ok", "complete"
