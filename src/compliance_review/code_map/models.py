from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from compliance_review.domain.models import Surface

CodeMapStatus = Literal["available", "unavailable", "degraded"]
GraphifyInitStatus = Literal["initialized", "unavailable", "degraded"]


class CodeMapModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class CodeMapQuery(CodeMapModel):
    query: str = Field(min_length=1)
    surface: Optional[Surface] = None
    max_candidates: int = Field(default=5, ge=1, le=20)
    budget: int = Field(default=2000, ge=100, le=10000)


class CodeMapPath(CodeMapModel):
    source: str = Field(min_length=1)
    target: str = Field(min_length=1)
    surface: Optional[Surface] = None
    max_hops: int = Field(default=6, ge=1, le=12)
    budget: int = Field(default=2000, ge=100, le=10000)


class CodeMapExplain(CodeMapModel):
    symbol: str = Field(min_length=1)
    surface: Optional[Surface] = None
    max_connections: int = Field(default=8, ge=1, le=20)
    budget: int = Field(default=2000, ge=100, le=10000)


class CodeMapNeighbors(CodeMapModel):
    symbol: str = Field(min_length=1)
    direction: Literal["callers", "callees"]
    surface: Optional[Surface] = None
    max_neighbors: int = Field(default=10, ge=1, le=20)
    budget: int = Field(default=2000, ge=100, le=10000)


class CodeMapImpact(CodeMapModel):
    symbol: str = Field(min_length=1)
    surface: Optional[Surface] = None
    max_nodes: int = Field(default=20, ge=1, le=40)
    depth: int = Field(default=2, ge=1, le=6)


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
    direction: Literal["outgoing", "incoming"] = "outgoing"
    confidence: Optional[str] = None
    source_path: Optional[str] = None
    source_line: Optional[int] = Field(default=None, ge=1)


class CodeMapQueryResult(CodeMapModel):
    query: str
    surface: Optional[Surface] = None
    status: CodeMapStatus
    provider: str = "graphify"
    navigation_only: Literal[True] = True
    candidates: list[CodeMapCandidate] = Field(default_factory=list)
    relations: list[CodeMapRelation] = Field(default_factory=list)
    error_code: Optional[str] = None
    truncated: bool = False


class CodeMapPathResult(CodeMapModel):
    source: str
    target: str
    surface: Optional[Surface] = None
    status: CodeMapStatus
    provider: str = "graphify"
    navigation_only: Literal[True] = True
    nodes: list[CodeMapCandidate] = Field(default_factory=list)
    relations: list[CodeMapRelation] = Field(default_factory=list)
    error_code: Optional[str] = None
    truncated: bool = False


class CodeMapExplainResult(CodeMapModel):
    symbol: str
    surface: Optional[Surface] = None
    status: CodeMapStatus
    provider: str = "graphify"
    navigation_only: Literal[True] = True
    node: Optional[CodeMapCandidate] = None
    relations: list[CodeMapRelation] = Field(default_factory=list)
    error_code: Optional[str] = None
    truncated: bool = False


class CodeMapNeighborsResult(CodeMapModel):
    symbol: str
    direction: Literal["callers", "callees"]
    surface: Optional[Surface] = None
    status: CodeMapStatus
    provider: str = "graphify"
    navigation_only: Literal[True] = True
    nodes: list[CodeMapCandidate] = Field(default_factory=list)
    relations: list[CodeMapRelation] = Field(default_factory=list)
    error_code: Optional[str] = None
    truncated: bool = False


class CodeMapImpactResult(CodeMapModel):
    symbol: str
    surface: Optional[Surface] = None
    status: CodeMapStatus
    provider: str = "graphify"
    navigation_only: Literal[True] = True
    nodes: list[CodeMapCandidate] = Field(default_factory=list)
    relations: list[CodeMapRelation] = Field(default_factory=list)
    error_code: Optional[str] = None
    truncated: bool = False


class GraphifyInitResult(CodeMapModel):
    repo_path: str
    status: GraphifyInitStatus
    graphify_command: Optional[str] = None
    build_command: list[str] = Field(default_factory=list)
    graph_paths: list[str] = Field(default_factory=list)
    error_code: Optional[str] = None
    message: Optional[str] = None
