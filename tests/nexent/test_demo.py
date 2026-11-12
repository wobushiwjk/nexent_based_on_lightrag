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


def test_run_chat_joins_delta_answers():
    """v2.8.0 streams fragments and ends with an empty complete marker."""
    north = FakeNorthbound(
        [
            {"type": "final_answer", "content": "\n", "answer_phase": "delta"},
            {"type": "final_answer", "content": "出口压力不足", "answer_phase": "delta"},
            {"type": "final_answer", "content": "，可能是过滤器堵塞。", "answer_phase": "delta"},
            {"type": "final_answer", "content": "", "answer_phase": "complete"},
        ]
    )

    result = run_chat(
        north,
        access_key="ak-demo",
        agent_name="industrial_fault_agent",
        query="question",
    )

    assert result["final_answer"] == "\n出口压力不足，可能是过滤器堵塞。"


def test_run_chat_keeps_only_the_last_step_answer():
    north = FakeNorthbound(
        [
            {"type": "final_answer", "content": "中间步骤", "answer_phase": "delta", "step_id": "s1"},
            {"type": "final_answer", "content": "最终回答", "answer_phase": "delta", "step_id": "s2"},
            {"type": "final_answer", "content": "", "answer_phase": "complete", "step_id": "s2"},
        ]
    )

    result = run_chat(
        north,
        access_key="ak-demo",
        agent_name="industrial_fault_agent",
        query="question",
    )

    assert result["final_answer"] == "最终回答"


def test_run_chat_prefers_non_empty_complete_answer():
    north = FakeNorthbound(
        [
            {"type": "final_answer", "content": "片段", "answer_phase": "delta"},
            {"type": "final_answer", "content": "完整回答", "answer_phase": "complete"},
        ]
    )

    result = run_chat(
        north,
        access_key="ak-demo",
        agent_name="industrial_fault_agent",
        query="question",
    )

    assert result["final_answer"] == "完整回答"


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
