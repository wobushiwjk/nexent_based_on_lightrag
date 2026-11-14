"""Tests for Nexent transcript ablation scoring."""

from __future__ import annotations

from scripts.evaluate_nexent_transcripts import evaluate_transcript


def test_three_agent_transcript_scores_evidence_and_subagents() -> None:
    transcript = {
        "agent_name": "industrial_fault_agent",
        "tenant_id": "tenant-1",
        "runs": [
            {
                "query": "P-101A 出口压力不足",
                "events": {
                    "tool": 3,
                    "tool_result": 3,
                    "worker_agent": 2,
                },
                "subagent_names": [
                    "evidence_review_agent",
                    "ontology_expert_agent",
                ],
                "errors": [],
                "final_answer": (
                    "入口过滤器堵塞 [evidence:EV-1] 叶轮汽蚀 [evidence:EV-2]"
                ),
            }
        ],
    }

    report = evaluate_transcript(
        transcript,
        required_phrases=["入口过滤器堵塞", "叶轮汽蚀"],
        expected_subagents=[
            "evidence_review_agent",
            "ontology_expert_agent",
        ],
    )

    assert report["all_runs_ok"] is True
    assert report["aggregate"]["evidence_citation_count"] == 2
    assert report["aggregate"]["unique_evidence_count"] == 2
    assert report["aggregate"]["tool_event_count"] == 6
    assert report["aggregate"]["subagent_event_count"] == 2


def test_dsml_marker_and_missing_subagent_make_ablation_fail() -> None:
    transcript = {
        "runs": [
            {
                "events": {},
                "errors": [],
                "final_answer": "<｜DSML｜ calls>",
            }
        ]
    }

    report = evaluate_transcript(
        transcript,
        required_phrases=["结论"],
        expected_subagents=["evidence_review_agent"],
    )

    run = report["runs"][0]
    assert report["all_runs_ok"] is False
    assert run["forbidden_phrase_hits"] == ["<｜DSML｜"]
    assert run["expected_subagents_missing"] == ["evidence_review_agent"]


def test_missing_expected_subagent_fails_without_protocol_error() -> None:
    report = evaluate_transcript(
        {
            "runs": [
                {
                    "query": "same question",
                    "events": {"tool": 2},
                    "errors": [],
                    "final_answer": "有证据 [evidence:EV-1]",
                }
            ]
        },
        expected_subagents=["evidence_review_agent"],
    )

    assert report["runs"][0]["expected_subagents_missing"] == ["evidence_review_agent"]
    assert report["all_runs_ok"] is False
