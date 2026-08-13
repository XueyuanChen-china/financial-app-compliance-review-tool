from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any, Protocol

from compliance_review.domain.models import ReviewResult
from compliance_review.review.models import ModelRequest, ModelResponse, ToolCall


class ModelProvider(Protocol):
    """Provider boundary used by ReviewerWorker; implementations may be local or remote."""

    def complete(self, request: ModelRequest) -> ModelResponse:
        """Return either structured content or bounded read-only tool calls."""


class StaticModelProvider:
    """Deterministic provider for tests and local pipeline smoke runs."""

    def __init__(self, response_factory: Any) -> None:
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
        timeout_seconds: float = 30.0,
    ) -> None:
        self.model = model
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY")
        self.base_url = base_url
        self.timeout_seconds = timeout_seconds

    def complete(self, request: ModelRequest) -> ModelResponse:
        if not self.api_key:
            raise RuntimeError("OPENAI_API_KEY is required for OpenAICompatibleProvider")
        body = {
            "model": self.model,
            "messages": request.messages,
            "tools": request.tools,
            "tool_choice": "auto",
            "temperature": 0,
        }
        if request.request_kind in {
            "obligation_extraction",
            "control_compilation",
            "verification",
        }:
            body["response_format"] = {"type": "json_object"}
        encoded = json.dumps(body).encode("utf-8")
        http_request = urllib.request.Request(
            self.base_url,
            data=encoded,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(http_request, timeout=self.timeout_seconds) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"model provider request failed: {exc}") from exc
        return _parse_chat_completion(payload)


def review_result_json(result: ReviewResult) -> str:
    """Serialize the validated result for providers that return JSON content."""
    return result.model_dump_json()


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
        provider_metadata={"id": payload.get("id")},
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
                        "max_hops": {"type": "integer", "minimum": 1, "maximum": 12},
                        "budget": {"type": "integer", "minimum": 100, "maximum": 10000},
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
                        "collector_id": {"type": "string"},
                        "fact_ids": {"type": "array", "items": {"type": "string"}},
                        "fact_type": {"type": "string"},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 100},
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
                        "pattern": {"type": "string"},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 100},
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
                        "root": {"type": "string"},
                        "file_globs": {"type": "array", "items": {"type": "string"}},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 100},
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
                        "start_line": {"type": "integer", "minimum": 1},
                        "line_count": {"type": "integer", "minimum": 1, "maximum": 300},
                    },
                },
            },
        },
    ]
