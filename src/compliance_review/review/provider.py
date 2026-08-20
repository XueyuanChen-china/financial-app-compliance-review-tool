from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from copy import deepcopy
from typing import Any, Protocol

from dotenv import load_dotenv

from compliance_review.domain.models import ReviewResult
from compliance_review.review.models import ModelRequest, ModelResponse, ToolCall

load_dotenv()

_REASONING_EFFORTS = {"none", "minimal", "low", "medium", "high", "xhigh"}
_STRUCTURED_MODES = {"json_schema", "json_object"}
_COMPILATION_REQUEST_KINDS = {
    "obligation_extraction",
    "control_compilation",
}
_STRUCTURED_REQUEST_KINDS = _COMPILATION_REQUEST_KINDS | {
    "applicability",
    "review_finalization",
    "verification",
    "impact",
}


class ModelProvider(Protocol):
    """Provider boundary used by ReviewerWorker; implementations may be local or remote."""

    def complete(self, request: ModelRequest) -> ModelResponse:
        """Return either structured content or bounded read-only tool calls."""


class StaticModelProvider:
    """Deterministic provider for tests and local pipeline smoke runs."""

    def __init__(self, response_factory: Any) -> None:
        self.supports_strict_finalization = False
        self.response_factory = response_factory
        self.requests: list[ModelRequest] = []

    def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        response = self.response_factory(request)
        if isinstance(response, ModelResponse):
            return response
        return ModelResponse.model_validate(response)


class OpenAICompatibleProvider:
    """Minimal OpenAI-compatible Chat Completions adapter.

    The adapter is intentionally transport-only. It does not decide compliance and
    does not write files. The worker owns tool execution and result validation.
    """

    def __init__(
        self,
        model: str,
        api_key: str | None = None,
        base_url: str = "https://api.openai.com/v1/chat/completions",
        timeout_seconds: float | None = None,
        compilation_timeout_seconds: float | None = None,
    ) -> None:
        self.supports_strict_finalization = True
        self.model = model
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY")
        self.base_url = base_url
        # Review workers can make multi-step tool calls and final structured
        # requests. Keep the transport timeout aligned with the runtime's
        # 180-second model-call budget instead of failing at urllib's old
        # 30-second default first.
        self.timeout_seconds = (
            timeout_seconds
            if timeout_seconds is not None
            else _env_float("COMPLIANCE_REVIEW_TIMEOUT_SECONDS", 180.0)
        )
        self.compilation_timeout_seconds = compilation_timeout_seconds or _env_float(
            "COMPLIANCE_COMPILATION_TIMEOUT_SECONDS", 120.0
        )
        self.actor_authorization = os.environ.get("COMPLIANCE_REVIEW_ACTOR_AUTHORIZATION")
        self.reasoning_effort = os.environ.get("COMPLIANCE_REVIEW_REASONING_EFFORT")
        self.compilation_reasoning_effort = os.environ.get(
            "COMPLIANCE_COMPILATION_REASONING_EFFORT", "low"
        )
        self.compilation_structured_mode = os.environ.get(
            "COMPLIANCE_COMPILATION_STRUCTURED_MODE", "json_schema"
        )
        self.obligation_structured_mode = os.environ.get(
            "COMPLIANCE_OBLIGATION_STRUCTURED_MODE", self.compilation_structured_mode
        )
        self.control_structured_mode = os.environ.get(
            "COMPLIANCE_CONTROL_STRUCTURED_MODE", self.compilation_structured_mode
        )
        if self.reasoning_effort and self.reasoning_effort not in _REASONING_EFFORTS:
            allowed = ", ".join(sorted(_REASONING_EFFORTS))
            raise ValueError("COMPLIANCE_REVIEW_REASONING_EFFORT must be one of: " + allowed)
        if self.compilation_reasoning_effort not in _REASONING_EFFORTS:
            allowed = ", ".join(sorted(_REASONING_EFFORTS))
            raise ValueError("COMPLIANCE_COMPILATION_REASONING_EFFORT must be one of: " + allowed)
        configured_modes = {
            "COMPLIANCE_COMPILATION_STRUCTURED_MODE": self.compilation_structured_mode,
            "COMPLIANCE_OBLIGATION_STRUCTURED_MODE": self.obligation_structured_mode,
            "COMPLIANCE_CONTROL_STRUCTURED_MODE": self.control_structured_mode,
        }
        invalid_modes = {
            name: value
            for name, value in configured_modes.items()
            if value not in _STRUCTURED_MODES
        }
        if invalid_modes:
            allowed = ", ".join(sorted(_STRUCTURED_MODES))
            raise ValueError(
                "structured compilation mode must be one of: "
                + allowed
                + " (invalid: "
                + ", ".join(f"{name}={value}" for name, value in invalid_modes.items())
                + ")"
            )
        if self.timeout_seconds <= 0:
            raise ValueError("COMPLIANCE_REVIEW_TIMEOUT_SECONDS must be positive")
        if self.compilation_timeout_seconds <= 0:
            raise ValueError("COMPLIANCE_COMPILATION_TIMEOUT_SECONDS must be positive")

    def complete(self, request: ModelRequest) -> ModelResponse:
        if not self.api_key:
            raise RuntimeError("OPENAI_API_KEY is required for OpenAICompatibleProvider")
        body = {
            "model": self.model,
            "messages": _normalize_chat_tool_turns(request.messages),
            "temperature": 0,
        }
        if request.tools:
            body["tools"] = request.tools
            body["tool_choice"] = "auto"
        reasoning_effort = self._reasoning_effort_for(
            request.request_kind, request.reasoning_effort_override
        )
        if reasoning_effort:
            body["reasoning_effort"] = reasoning_effort
        # Structured requests keep their JSON Schema even when the model also
        # has read-only tools available. The model may use tools first, but its
        # eventual content is still constrained and locally validated.
        if request.request_kind in _STRUCTURED_REQUEST_KINDS:
            if request.response_schema:
                if self._structured_mode_for(request.request_kind) == "json_schema":
                    body["response_format"] = {
                        "type": "json_schema",
                        "json_schema": {
                            "name": request.request_kind,
                            "strict": True,
                            "schema": _strict_json_schema(request.response_schema),
                        },
                    }
                else:
                    body["response_format"] = {"type": "json_object"}
            else:
                body["response_format"] = {"type": "json_object"}
        encoded = json.dumps(body).encode("utf-8")
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        if self.actor_authorization:
            headers["x-openai-actor-authorization"] = self.actor_authorization
        http_request = urllib.request.Request(
            self.base_url,
            data=encoded,
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(
                http_request, timeout=self._timeout_for(request.request_kind)
            ) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            try:
                detail = exc.read().decode("utf-8", errors="replace")
            except OSError:
                detail = "<unavailable>"
            raise RuntimeError(f"model provider request failed: HTTP {exc.code}: {detail}") from exc
        except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"model provider request failed: {exc}") from exc
        return _parse_chat_completion(payload)

    def _reasoning_effort_for(
        self, request_kind: str, override: str | None = None
    ) -> str | None:
        if override:
            return override
        if request_kind in _COMPILATION_REQUEST_KINDS:
            return self.compilation_reasoning_effort
        return self.reasoning_effort

    def _timeout_for(self, request_kind: str) -> float:
        if request_kind in _COMPILATION_REQUEST_KINDS:
            return self.compilation_timeout_seconds
        return self.timeout_seconds

    def _structured_mode_for(self, request_kind: str) -> str:
        if request_kind == "obligation_extraction":
            return self.obligation_structured_mode
        if request_kind == "control_compilation":
            return self.control_structured_mode
        return os.environ.get("COMPLIANCE_VERIFICATION_STRUCTURED_MODE", "json_schema")


def _env_float(name: str, default: float) -> float:
    raw_value = os.environ.get(name)
    if raw_value is None or not raw_value.strip():
        return default
    try:
        return float(raw_value)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number") from exc


def review_result_json(result: ReviewResult) -> str:
    """Serialize the validated result for providers that return JSON content."""
    return result.model_dump_json()


def _strict_json_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Normalize Pydantic JSON Schema for strict structured-output providers.

    Strict JSON Schema endpoints require every object property to be listed in
    ``required``. Pydantic omits fields with defaults from that list, so the
    provider must normalize the transport schema without changing the domain
    model's validation rules.
    """
    normalized = deepcopy(schema)

    def visit(node: Any) -> None:
        if isinstance(node, dict):
            properties = node.get("properties")
            if isinstance(properties, dict):
                node["required"] = list(properties)
                node["additionalProperties"] = False
                for child in properties.values():
                    visit(child)
            for key in ("$defs", "definitions"):
                definitions = node.get(key)
                if isinstance(definitions, dict):
                    for child in definitions.values():
                        visit(child)
            for key in ("items", "additionalProperties", "not"):
                child = node.get(key)
                if isinstance(child, dict):
                    visit(child)
            for key in ("anyOf", "allOf", "oneOf", "prefixItems"):
                children = node.get(key)
                if isinstance(children, list):
                    for child in children:
                        visit(child)
        elif isinstance(node, list):
            for child in node:
                visit(child)

    visit(normalized)
    return normalized


def _normalize_chat_tool_turns(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return a Chat Completions-valid transcript for function-call turns.

    A tool result is valid only when it follows an assistant turn that declared
    its call id.  Compatibility proxies commonly surface this as a vague 400,
    so reject malformed transcripts locally and make function-call content
    explicit for providers that require ``content: null``.
    """

    normalized: list[dict[str, Any]] = []
    pending_call_ids: set[str] = set()
    for index, message in enumerate(messages):
        current = deepcopy(message)
        role = current.get("role")
        if role == "assistant":
            raw_calls = current.get("tool_calls")
            if isinstance(raw_calls, list) and raw_calls:
                current.setdefault("content", None)
                pending_call_ids = {
                    str(call.get("id"))
                    for call in raw_calls
                    if isinstance(call, dict) and isinstance(call.get("id"), str)
                }
                if not pending_call_ids:
                    raise ValueError(
                        f"assistant tool-call message at index {index} has no valid call ids"
                    )
            elif pending_call_ids:
                raise ValueError(
                    "assistant message encountered before all prior tool calls received outputs"
                )
        elif role == "tool":
            call_id = current.get("tool_call_id")
            if not isinstance(call_id, str) or call_id not in pending_call_ids:
                raise ValueError(
                    f"tool result at index {index} does not match a prior assistant tool call"
                )
            pending_call_ids.remove(call_id)
        elif pending_call_ids:
            raise ValueError(
                "non-tool message encountered before all prior tool calls received outputs"
            )
        normalized.append(current)
    if pending_call_ids:
        raise ValueError("assistant tool-call message is missing one or more tool outputs")
    return normalized


def _parse_chat_completion(payload: Any) -> ModelResponse:
    if not isinstance(payload, dict):
        raise RuntimeError("model provider response must be a JSON object")
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        raise RuntimeError("model provider response has no choices")
    first = choices[0]
    if not isinstance(first, dict) or not isinstance(first.get("message"), dict):
        raise RuntimeError("model provider response has no message")
    message = first["message"]
    tool_calls: list[ToolCall] = []
    raw_tool_calls = message.get("tool_calls", [])
    if isinstance(raw_tool_calls, list):
        for raw_call in raw_tool_calls:
            if not isinstance(raw_call, dict):
                continue
            function = raw_call.get("function")
            if not isinstance(function, dict):
                continue
            arguments = function.get("arguments", {})
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except json.JSONDecodeError:
                    arguments = {}
            if not isinstance(arguments, dict):
                arguments = {}
            tool_calls.append(
                ToolCall(
                    call_id=str(raw_call.get("id", "unknown-call")),
                    name=function.get("name", "read_file"),
                    arguments=arguments,
                )
            )
    usage = payload.get("usage")
    usage_dict = usage if isinstance(usage, dict) else {}
    return ModelResponse(
        content=message.get("content") if isinstance(message.get("content"), str) else None,
        tool_calls=tool_calls,
        input_tokens=int(usage_dict.get("prompt_tokens", 0) or 0),
        output_tokens=int(usage_dict.get("completion_tokens", 0) or 0),
        provider_metadata={
            "id": payload.get("id"),
            "usage_available": (
                isinstance(usage, dict)
                and "prompt_tokens" in usage
                and "completion_tokens" in usage
            ),
        },
    )


def tool_schemas() -> list[dict[str, Any]]:
    """Return the only tools a Reviewer may request."""
    return [
        {
            "type": "function",
            "function": {
                "name": "code_map_query",
                "description": (
                    "Query the bounded Graphify code map for candidate symbols and relations."
                ),
                "parameters": {
                    "type": "object",
                    "required": ["query"],
                    "properties": {
                        "query": {"type": "string"},
                        "surface": {"type": "string"},
                        "repository_id": {"type": "string"},
                        "max_candidates": {"type": "integer", "minimum": 1, "maximum": 20},
                        "budget": {"type": "integer", "minimum": 100, "maximum": 10000},
                    },
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "code_map_path",
                "description": "Find a bounded Graphify relation path between two symbols.",
                "parameters": {
                    "type": "object",
                    "required": ["source", "target"],
                    "properties": {
                        "source": {"type": "string"},
                        "target": {"type": "string"},
                        "surface": {"type": "string"},
                        "repository_id": {"type": "string"},
                        "max_hops": {"type": "integer", "minimum": 1, "maximum": 12},
                        "budget": {"type": "integer", "minimum": 100, "maximum": 10000},
                    },
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "code_map_explain",
                "description": "Explain one Graphify symbol and show bounded directed connections.",
                "parameters": {
                    "type": "object",
                    "required": ["symbol"],
                    "properties": {
                        "symbol": {"type": "string"},
                        "surface": {"type": "string"},
                        "repository_id": {"type": "string"},
                        "max_connections": {"type": "integer", "minimum": 1, "maximum": 20},
                        "budget": {"type": "integer", "minimum": 100, "maximum": 10000},
                    },
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "code_map_callers",
                "description": (
                    "Find bounded incoming call/reference relations for one Graphify symbol."
                ),
                "parameters": {
                    "type": "object",
                    "required": ["symbol"],
                    "properties": {
                        "symbol": {"type": "string"},
                        "surface": {"type": "string"},
                        "repository_id": {"type": "string"},
                        "max_neighbors": {"type": "integer", "minimum": 1, "maximum": 20},
                        "budget": {"type": "integer", "minimum": 100, "maximum": 10000},
                    },
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "code_map_callees",
                "description": (
                    "Find bounded outgoing call/reference relations for one Graphify symbol."
                ),
                "parameters": {
                    "type": "object",
                    "required": ["symbol"],
                    "properties": {
                        "symbol": {"type": "string"},
                        "surface": {"type": "string"},
                        "repository_id": {"type": "string"},
                        "max_neighbors": {"type": "integer", "minimum": 1, "maximum": 20},
                        "budget": {"type": "integer", "minimum": 100, "maximum": 10000},
                    },
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "code_map_impact",
                "description": (
                    "Find bounded reverse-impact candidates using Graphify affected traversal."
                ),
                "parameters": {
                    "type": "object",
                    "required": ["symbol"],
                    "properties": {
                        "symbol": {"type": "string"},
                        "surface": {"type": "string"},
                        "repository_id": {"type": "string"},
                        "max_nodes": {"type": "integer", "minimum": 1, "maximum": 40},
                        "depth": {"type": "integer", "minimum": 1, "maximum": 6},
                    },
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "capture_anchor",
                "description": (
                    "Capture an exact source range as a verified immutable evidence anchor. "
                    "Use only after navigation and read_file verification."
                ),
                "parameters": {
                    "type": "object",
                    "required": ["path", "start_line", "end_line"],
                    "properties": {
                        "path": {"type": "string"},
                        "surface": {"type": "string"},
                        "repository_id": {"type": "string"},
                        "start_line": {"type": "integer", "minimum": 1},
                        "end_line": {"type": "integer", "minimum": 1},
                    },
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_collector_facts",
                "description": "Read precomputed deterministic Manifest, dependency, or API facts.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "surface": {"type": "string"},
                        "repository_id": {"type": "string"},
                        "collector_id": {"type": "string"},
                        "fact_ids": {"type": "array", "items": {"type": "string"}},
                        "fact_type": {"type": "string"},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 30},
                    },
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "list_files",
                "description": "List files inside the work item's allowed roots.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "surface": {"type": "string"},
                        "repository_id": {"type": "string"},
                        "pattern": {"type": "string"},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 60},
                    },
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "search_code",
                "description": "Search exact text inside the work item's allowed roots.",
                "parameters": {
                    "type": "object",
                    "required": ["query"],
                    "properties": {
                        "query": {"type": "string"},
                        "surface": {"type": "string"},
                        "repository_id": {"type": "string"},
                        "root": {"type": "string"},
                        "file_globs": {"type": "array", "items": {"type": "string"}},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 40},
                    },
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "read_file",
                "description": "Read a bounded line range inside the work item's allowed roots.",
                "parameters": {
                    "type": "object",
                    "required": ["path"],
                    "properties": {
                        "path": {"type": "string"},
                        "surface": {"type": "string"},
                        "repository_id": {"type": "string"},
                        "start_line": {"type": "integer", "minimum": 1},
                        "line_count": {"type": "integer", "minimum": 1, "maximum": 200},
                    },
                },
            },
        },
    ]
