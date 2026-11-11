"""Offline tests for the northbound demo transcript."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from nexent.scripts.demo import DemoError, DemoOptions, run_chat, run_demo


class FakeManagement:
    def signin(self, email, password):
        return {}

    def current_user(self):
        return {"user_id": "u-1", "tenant_id": "tenant-1"}

    def ensure_api_key(self, user_id):
        assert user_id == "u-1"
        return "ak-demo"


class FakeNorthbound:
    def __init__(self, events):
        self.events = events
        self.requests = []

    def stream_events(self, method, path, *, json_body=None, token=None):
        self.requests.append(
            {
                "method": method,
                "path": path,
                "json_body": json_body,
                "token": token,
            }
        )
        yield from self.events


def test_run_chat_collects_events_and_answer():
    north = FakeNorthbound(
        [
            {"type": "conversation_created", "content": {"conversation_id": 42}},
            {"type": "tool", "content": {"name": "trace_evidence"}},
            {"type": "final_answer", "content": "answer with [evidence:EV-1]"},
        ]
    )

    result = run_chat(
        north,
        access_key="ak-demo",
        agent_name="industrial_fault_agent",
        query="question",
    )

    assert result["conversation_id"] == 42
    assert result["final_answer"] == "answer with [evidence:EV-1]"
    assert result["events"] == {
        "conversation_created": 1,
        "tool": 1,
        "final_answer": 1,
    }
    request = north.requests[0]
    assert request["path"] == "/nb/v1/chat/run"
    assert request["token"] == "ak-demo"
    assert request["json_body"]["final_answer_mode"] == "complete"
    assert request["json_body"]["agent_name"] == "industrial_fault_agent"


def test_run_chat_without_answer_raises():
    north = FakeNorthbound([{"type": "error", "content": "boom"}])

    with pytest.raises(DemoError) as info:
        run_chat(
            north,
            access_key="ak-demo",
            agent_name="industrial_fault_agent",
            query="question",
        )

    assert "no final_answer" in str(info.value)


def test_run_demo_writes_transcript(tmp_path):
    north = FakeNorthbound(
        [{"type": "final_answer", "content": "第一条回答"}]
    )
    options = DemoOptions(
        agent_name="industrial_fault_agent",
        queries=("问题一", "问题二"),
        out_dir=tmp_path,
    )

    transcript = run_demo(
        options,
        client=FakeManagement(),
        northbound=north,
        out=lambda line: None,
    )

    path = tmp_path / Path(transcript["transcript_path"]).name
    written = json.loads(path.read_text(encoding="utf-8"))
    assert written["tenant_id"] == "tenant-1"
    assert [run["query"] for run in written["runs"]] == ["问题一", "问题二"]
    assert written["runs"][0]["final_answer"] == "第一条回答"
    assert len(north.requests) == 2
