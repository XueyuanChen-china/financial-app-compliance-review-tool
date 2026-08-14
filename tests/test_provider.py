from __future__ import annotations

import json
import urllib.request
from typing import Any

import pytest

from compliance_review.compilation.models import ControlDraftSetTransport
from compliance_review.domain.models import WorkItem
from compliance_review.review.models import ModelRequest
from compliance_review.review.provider import OpenAICompatibleProvider, _strict_json_schema


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

    assert captured["timeout"] == 30.0
    assert captured["body"]["reasoning_effort"] == "high"
    assert captured["body"]["tools"]
    assert captured["body"]["tool_choice"] == "auto"


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
