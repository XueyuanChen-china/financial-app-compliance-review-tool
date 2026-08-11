from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from compliance_review.domain.models import Surface

CodeMapStatus = Literal["available", "unavailable", "degraded"]


class CodeMapModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class CodeMapQuery(CodeMapModel):
    query: str = Field(min_length=1)
    surface: Optional[Surface] = None
    max_candidates: int = Field(default=5, ge=1, le=20)
    budget: int = Field(default=2000, ge=100, le=10000)


class CodeMapCandidate(CodeMapModel):
    symbol: str = Field(min_length=1)
    path: Optional[str] = None
    start_line: Optional[int] = Field(default=None, ge=1)
    end_line: Optional[int] = Field(default=None, ge=1)
    score: Optional[float] = Field(default=None, ge=0, le=1)


class CodeMapRelation(CodeMapModel):
    source: str = Field(min_length=1)
    relation: str = Field(min_length=1)
    target: str = Field(min_length=1)
    confidence: Optional[str] = None
    source_path: Optional[str] = None
    source_line: Optional[int] = Field(default=None, ge=1)


class CodeMapQueryResult(CodeMapModel):
    query: str
    surface: Optional[Surface] = None
    status: CodeMapStatus
    provider: str = "graphify"
    candidates: list[CodeMapCandidate] = Field(default_factory=list)
    relations: list[CodeMapRelation] = Field(default_factory=list)
    error_code: Optional[str] = None
    truncated: bool = False
