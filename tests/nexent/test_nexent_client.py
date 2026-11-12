"""Offline tests for the stdlib Nexent client.

The transport is scripted, so these tests pin request construction and
response handling without a live Nexent instance.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from nexent.scripts.nexent_client import (
    HttpResponse,
    NexentClient,
    NexentConfigError,
    NexentError,
    UrllibTransport,
    encode_multipart,
    parse_sse_events,
    redact,
)


def json_response(payload: Any, status: int = 200) -> HttpResponse:
    return HttpResponse(
        status=status,
        headers={"content-type": "application/json"},
        body=json.dumps(payload).encode("utf-8"),
    )


class ScriptedTransport:
    """Return canned responses in order and record every request."""

    def __init__(self, *responses: HttpResponse | Exception) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def __call__(self, method, url, headers, body):
        self.calls.append(
            {
                "method": method,
                "url": url,
                "headers": dict(headers),
                "body": body,
            }
        )
        if not self._responses:
            raise AssertionError(f"unexpected request: {method} {url}")
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def test_request_builds_json_body_and_auth_header():
    transport = ScriptedTransport(json_response({"ok": True}))
    client = NexentClient("http://nexent:5010/", transport=transport)
    client.jwt = "jwt-token"

    payload = client.request("POST", "/thing", json_body={"a": 1})

    assert payload == {"ok": True}
    assert client.base_url == "http://nexent:5010"
    call = transport.calls[0]
    assert call["method"] == "POST"
    assert call["url"] == "http://nexent:5010/thing"
    assert call["headers"]["Authorization"] == "Bearer jwt-token"
    assert call["headers"]["Content-Type"] == "application/json"
    assert json.loads(call["body"]) == {"a": 1}


def test_request_raises_with_status_and_detail():
    transport = ScriptedTransport(json_response({"detail": "no such agent"}, 404))
    client = NexentClient(transport=transport)

    with pytest.raises(NexentError) as info:
        client.request("GET", "/agent")

    assert info.value.status == 404
    assert info.value.detail == "no such agent"


def test_request_accepts_expected_statuses():
    transport = ScriptedTransport(json_response({"detail": "gone"}, 404))
    client = NexentClient(transport=transport)

    assert client.request("GET", "/x", expected=(200, 404)) == {"detail": "gone"}


def test_signin_remembers_token():
    transport = ScriptedTransport(
        json_response({"data": {"session": {"access_token": "jwt-1"}}})
    )
    client = NexentClient(transport=transport)

    client.signin("admin@example.com", "secret")

    assert client.jwt == "jwt-1"
    call = transport.calls[0]
    assert call["url"].endswith("/user/signin")
    assert json.loads(call["body"]) == {
        "email": "admin@example.com",
        "password": "secret",
    }
    assert "Authorization" not in call["headers"]


def test_signin_without_token_raises():
    transport = ScriptedTransport(json_response({"data": {}}))
    client = NexentClient(transport=transport)

    with pytest.raises(NexentConfigError):
        client.signin("admin@example.com", "secret")


def test_ensure_api_key_reuses_unmasked_key():
    transport = ScriptedTransport(
        json_response({"data": [{"access_key": "ak-real", "can_copy": True}]})
    )
    client = NexentClient(transport=transport)

    assert client.ensure_api_key("user-1") == "ak-real"
    assert len(transport.calls) == 1


def test_ensure_api_key_creates_when_masked():
    transport = ScriptedTransport(
        json_response({"data": [{"access_key": "ak-****", "can_copy": False}]}),
        json_response({"data": {"access_key": "ak-new"}}),
    )
    client = NexentClient(transport=transport)

    assert client.ensure_api_key("user-1") == "ak-new"
    assert transport.calls[1]["method"] == "POST"
    assert transport.calls[1]["url"].endswith("/user/tokens")


def test_get_agent_by_name_returns_none_on_404():
    transport = ScriptedTransport(json_response({"detail": "not found"}, 404))
    client = NexentClient(transport=transport)

    assert client.get_agent_by_name("industrial_fault_agent") is None

    transport = ScriptedTransport(
        json_response({"agent_id": 7, "latest_version_no": 2})
    )
    client = NexentClient(transport=transport)

    assert client.get_agent_by_name("industrial_fault_agent") == {
        "agent_id": 7,
        "latest_version_no": 2,
    }


def test_upload_files_propagates_http_errors(tmp_path):
    document = tmp_path / "policy.md"
    document.write_text("# policy\n", encoding="utf-8")
    transport = ScriptedTransport(json_response({"detail": "too large"}, 400))
    client = NexentClient(transport=transport)

    with pytest.raises(NexentError) as info:
        client.upload_files("kb-index", [document])

    assert info.value.status == 400


def test_process_files_accepts_created_status():
    """Nexent returns 201 for the task-submission route, not an error."""
    payload = {
        "message": "Files processing triggered successfully",
        "process_tasks": {"status": "success", "task_ids": ["t1"]},
    }
    transport = ScriptedTransport(json_response(payload, 201))
    client = NexentClient(transport=transport)

    result = client.process_files(
        "kb-index",
        [{"path_or_url": "knowledge_base/x.md", "filename": "x.md"}],
    )

    assert result == payload


def test_find_index_by_display_name_matches_internal_name():
    transport = ScriptedTransport(
        json_response(
            {
                "indices_info": [
                    {"name": "kb-uuid", "display_name": "industrial-fault-kb"},
                    {"name": "other"},
                ]
            }
        )
    )
    client = NexentClient(transport=transport)

    assert client.find_index_by_display_name("industrial-fault-kb")["name"] == "kb-uuid"
    # Older builds expose only ``name``; the display name may equal it.
    transport = ScriptedTransport(json_response({"indices_info": [{"name": "kb"}]}))
    client = NexentClient(transport=transport)
    assert client.find_index_by_display_name("kb")["name"] == "kb"


def test_resolve_embedding_model_prefers_display_name():
    transport = ScriptedTransport(
        json_response(
            {
                "data": [
                    {"model_id": 1, "display_name": "bge-m3", "model_type": "embedding"},
                    {"model_id": 2, "display_name": "text-embedding-3", "model_type": "embedding"},
                    {"model_id": 3, "display_name": "qwen", "model_type": "llm"},
                ]
            }
        )
    )
    client = NexentClient(transport=transport)

    assert client.resolve_embedding_model("text-embedding-3")["model_id"] == 2


def test_resolve_embedding_model_rejects_unknown_name():
    transport = ScriptedTransport(
        json_response(
            {
                "data": [
                    {"model_id": 1, "display_name": "bge-m3", "model_type": "embedding"}
                ]
            }
        )
    )
    client = NexentClient(transport=transport)

    with pytest.raises(NexentConfigError) as info:
        client.resolve_embedding_model("missing")

    assert "bge-m3" in str(info.value)


def test_resolve_embedding_model_requires_a_model():
    transport = ScriptedTransport(json_response({"data": []}))
    client = NexentClient(transport=transport)

    with pytest.raises(NexentConfigError):
        client.resolve_embedding_model()


def test_parse_sse_events_ignores_noise_and_bad_json():
    lines = [
        b"event: message\n",
        b'data: {"type": "conversation_created", "content": {"conversation_id": 3}}\n',
        b"data: [DONE]\n",
        b"data: not-json\n",
        b'data: {"type": "final_answer", "content": "ok"}',
    ]

    events = list(parse_sse_events(lines))

    assert [event["type"] for event in events] == [
        "conversation_created",
        "final_answer",
    ]


def test_stream_events_defaults_to_the_streaming_method(monkeypatch):
    """The default SSE path must call ``UrllibTransport.stream``, not ``__call__``."""
    captured: dict[str, Any] = {}

    def fake_stream(self, method, url, headers, body):
        captured.update(method=method, url=url, headers=dict(headers), body=body)
        return iter([b'data: {"type": "final_answer", "content": "ok"}\n'])

    monkeypatch.setattr(UrllibTransport, "stream", fake_stream)
    client = NexentClient("http://nexent:5010")
    client.jwt = "jwt-token"

    events = list(
        client.stream_events("POST", "/nb/v1/chat/run", json_body={"q": 1})
    )

    assert events == [{"type": "final_answer", "content": "ok"}]
    assert captured["url"] == "http://nexent:5010/nb/v1/chat/run"
    assert captured["headers"]["Accept"] == "text/event-stream"
    assert json.loads(captured["body"]) == {"q": 1}


def test_encode_multipart_is_deterministic():
    fields = [("index_name", "kb")]
    files = [("file", "a.md", b"# a\n", "text/markdown")]

    first_body, first_type = encode_multipart(fields, files)
    second_body, second_type = encode_multipart(fields, files)

    assert first_body == second_body
    assert first_type == second_type
    assert b'name="index_name"' in first_body
    assert b'name="file"; filename="a.md"' in first_body
    assert b"# a\n" in first_body
    assert first_body.endswith(b"--\r\n")


def test_redact_masks_secrets_recursively():
    payload = {
        "access_key": "ak-real",
        "nested": {"password": "secret", "user": "admin"},
        "items": [{"api_key": "k"}],
        "empty_secret": "",
    }

    redacted = redact(payload)

    assert redacted["access_key"] == "***"
    assert redacted["nested"]["password"] == "***"
    assert redacted["nested"]["user"] == "admin"
    assert redacted["items"][0]["api_key"] == "***"
    assert redacted["empty_secret"] == ""


def test_healthcheck_unwraps_health_status():
    transport = ScriptedTransport(
        json_response({"data": {"health_status": "healthy"}})
    )
    client = NexentClient(transport=transport)

    assert client.healthcheck_mcp(4) == "healthy"
    assert transport.calls[0]["url"].endswith("/mcp/healthcheck?mcp_id=4")
