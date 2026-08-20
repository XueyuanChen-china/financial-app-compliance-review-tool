from __future__ import annotations

import json
import urllib.request
from typing import Any

import pytest

from compliance_review.compilation.models import ControlDraftSetTransport
from compliance_review.domain.models import WorkItem
from compliance_review.review.models import ModelRequest
from compliance_review.review.provider import (
    OpenAICompatibleProvider,
    _normalize_chat_tool_turns,
    _strict_json_schema,
)


class _FakeHTTPResponse:
    def __enter__(self) -> _FakeHTTPResponse:
        return self

    def __exit__(self, *args: Any) -> None:
        return None

    def read(self) -> bytes:
        return b'{"choices":[{"message":{"content":"{\\"ok\\":true}"}}]}'


def _request(
    request_kind: str,
    *,
    tools: list[dict[str, Any]] | None = None,
    response_schema: dict[str, Any] | None = None,
    reasoning_effort_override: str | None = None,
) -> ModelRequest:
    work_item = WorkItem(
        work_item_id="provider-test",
        module_id="provider",
        surface="android_native",
        control_ids=["control.test"],
    )
    return ModelRequest(
        work_item=work_item,
        attempt_id="attempt-1",
        agent_id="test-agent",
        messages=[{"role": "user", "content": "test"}],
        tools=tools or [],
        request_kind=request_kind,  # type: ignore[arg-type]
        response_schema=response_schema,
        reasoning_effort_override=reasoning_effort_override,  # type: ignore[arg-type]
    )


def test_compilation_request_uses_bounded_compatible_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def fake_urlopen(request: urllib.request.Request, timeout: float) -> _FakeHTTPResponse:
        captured["body"] = json.loads(request.data.decode("utf-8"))
        captured["timeout"] = timeout
        return _FakeHTTPResponse()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setenv("COMPLIANCE_COMPILATION_REASONING_EFFORT", "low")
    monkeypatch.setenv("COMPLIANCE_COMPILATION_TIMEOUT_SECONDS", "120")
    monkeypatch.setenv("COMPLIANCE_COMPILATION_STRUCTURED_MODE", "json_schema")

    provider = OpenAICompatibleProvider("test-model", api_key="test-key")
    provider.complete(
        _request(
            "obligation_extraction",
            response_schema={
                "type": "object",
                "properties": {"ok": {"type": "boolean"}},
            },
        )
    )

    body = captured["body"]
    assert captured["timeout"] == 120.0
    assert body["reasoning_effort"] == "low"
    assert body["response_format"]["type"] == "json_schema"
    assert body["response_format"]["json_schema"]["schema"]["required"] == ["ok"]
    assert "tools" not in body
    assert "tool_choice" not in body


def test_reasoning_effort_override_is_used_for_navigation_rounds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def fake_urlopen(request: urllib.request.Request, timeout: float) -> _FakeHTTPResponse:
        captured["body"] = json.loads(request.data.decode("utf-8"))
        return _FakeHTTPResponse()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setenv("COMPLIANCE_REVIEW_REASONING_EFFORT", "high")

    provider = OpenAICompatibleProvider("test-model", api_key="test-key")
    provider.complete(
        _request("review", reasoning_effort_override="medium")
    )

    assert captured["body"]["reasoning_effort"] == "medium"


def test_compilation_can_use_explicit_json_object_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def fake_urlopen(request: urllib.request.Request, timeout: float) -> _FakeHTTPResponse:
        captured["body"] = json.loads(request.data.decode("utf-8"))
        return _FakeHTTPResponse()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setenv("COMPLIANCE_CONTROL_STRUCTURED_MODE", "json_object")

    provider = OpenAICompatibleProvider("test-model", api_key="test-key")
    provider.complete(
        _request(
            "control_compilation",
            response_schema={
                "type": "object",
                "properties": {"ok": {"type": "boolean"}},
            },
        )
    )

    assert captured["body"]["response_format"] == {"type": "json_object"}


def test_compilation_modes_can_differ_by_stage(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[dict[str, Any]] = []

    def fake_urlopen(request: urllib.request.Request, timeout: float) -> _FakeHTTPResponse:
        captured.append(json.loads(request.data.decode("utf-8")))
        return _FakeHTTPResponse()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setenv("COMPLIANCE_COMPILATION_STRUCTURED_MODE", "json_schema")
    monkeypatch.setenv("COMPLIANCE_CONTROL_STRUCTURED_MODE", "json_object")

    provider = OpenAICompatibleProvider("test-model", api_key="test-key")
    schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}}
    provider.complete(_request("obligation_extraction", response_schema=schema))
    provider.complete(_request("control_compilation", response_schema=schema))

    assert captured[0]["response_format"]["type"] == "json_schema"
    assert captured[1]["response_format"] == {"type": "json_object"}


def test_review_keeps_tools_and_review_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def fake_urlopen(request: urllib.request.Request, timeout: float) -> _FakeHTTPResponse:
        captured["body"] = json.loads(request.data.decode("utf-8"))
        captured["timeout"] = timeout
        return _FakeHTTPResponse()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setenv("COMPLIANCE_REVIEW_REASONING_EFFORT", "high")
    monkeypatch.setenv("COMPLIANCE_REVIEW_TIMEOUT_SECONDS", "180")
    monkeypatch.setenv("COMPLIANCE_COMPILATION_REASONING_EFFORT", "low")

    provider = OpenAICompatibleProvider("test-model", api_key="test-key")
    provider.complete(
        _request(
            "review",
            tools=[
                {
                    "type": "function",
                    "function": {"name": "read_file", "parameters": {}},
                }
            ],
        )
    )

    assert captured["timeout"] == 180.0
    assert captured["body"]["reasoning_effort"] == "high"
    assert captured["body"]["tools"]
    assert captured["body"]["tool_choice"] == "auto"


def test_review_timeout_can_be_explicitly_overridden(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def fake_urlopen(request: urllib.request.Request, timeout: float) -> _FakeHTTPResponse:
        del request
        captured["timeout"] = timeout
        return _FakeHTTPResponse()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    provider = OpenAICompatibleProvider("test-model", api_key="test-key", timeout_seconds=45)
    provider.complete(_request("review"))

    assert captured["timeout"] == 45


def test_tool_turns_include_explicit_null_content_for_compatibility_proxies() -> None:
    messages = _normalize_chat_tool_turns(
        [
            {"role": "user", "content": "inspect the repository"},
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "call-1",
                        "type": "function",
                        "function": {"name": "read_file", "arguments": "{}"},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call-1", "content": "{}"},
        ]
    )

    assert messages[1]["content"] is None
    assert messages[2]["tool_call_id"] == "call-1"


def test_orphan_tool_output_is_rejected_before_transport() -> None:
    with pytest.raises(ValueError, match="does not match a prior assistant tool call"):
        _normalize_chat_tool_turns(
            [
                {"role": "user", "content": "inspect the repository"},
                {"role": "tool", "tool_call_id": "orphan", "content": "{}"},
            ]
        )


def test_applicability_keeps_tools_and_structured_response_format(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def fake_urlopen(request: urllib.request.Request, timeout: float) -> _FakeHTTPResponse:
        captured["body"] = json.loads(request.data.decode("utf-8"))
        return _FakeHTTPResponse()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setenv("COMPLIANCE_VERIFICATION_STRUCTURED_MODE", "json_schema")

    provider = OpenAICompatibleProvider("test-model", api_key="test-key")
    provider.complete(
        _request(
            "applicability",
            tools=[
                {
                    "type": "function",
                    "function": {"name": "search_code", "parameters": {}},
                }
            ],
            response_schema={
                "type": "object",
                "properties": {"decision": {"type": "string"}},
            },
        )
    )

    body = captured["body"]
    assert body["tools"]
    assert body["tool_choice"] == "auto"
    assert body["response_format"]["type"] == "json_schema"
    assert body["response_format"]["json_schema"]["strict"] is True


def test_invalid_compilation_transport_configuration_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("COMPLIANCE_COMPILATION_STRUCTURED_MODE", "plain_text")
    with pytest.raises(ValueError, match="COMPLIANCE_COMPILATION_STRUCTURED_MODE"):
        OpenAICompatibleProvider("test-model", api_key="test-key")


def test_control_transport_schema_avoids_dynamic_property_names() -> None:
    schema = _strict_json_schema(ControlDraftSetTransport.model_json_schema())
    serialized = json.dumps(schema)

    assert "propertyNames" not in serialized
    definitions = schema["$defs"]
    transport_schema = definitions["ControlDraftTransport"]
    evidence_requirements = transport_schema["properties"]["evidence_requirements"]
    assert evidence_requirements["type"] == "array"
