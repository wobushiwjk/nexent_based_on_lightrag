"""Score recorded Nexent transcripts for single-Agent/three-Agent ablations."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any, Iterable, Mapping

_EVIDENCE_RE = re.compile(r"\[evidence:([A-Za-z0-9._:-]+)\]")


def evaluate_transcript(
    transcript: Mapping[str, Any],
    *,
    required_phrases: Iterable[str] = (),
    forbidden_phrases: Iterable[str] = ("<|DSML|", "<｜DSML｜"),
    expected_subagents: Iterable[str] = (),
) -> dict[str, Any]:
    """Compute repeatable quality signals from one demo transcript."""

    runs = [item for item in transcript.get("runs") or [] if isinstance(item, Mapping)]
    sample_reports: list[dict[str, Any]] = []
    for run in runs:
        answer = str(run.get("final_answer") or "")
        evidence_ids = sorted(set(_EVIDENCE_RE.findall(answer)))
        serialized = json.dumps(run, ensure_ascii=False)
        events = run.get("events")
        events = events if isinstance(events, Mapping) else {}
        tool_events = sum(
            int(value)
            for name, value in events.items()
            if "tool" in str(name).casefold() and isinstance(value, int)
        )
        subagent_events = sum(
            int(value)
            for name, value in events.items()
            if "subagent" in str(name).casefold()
            or "worker_agent" in str(name).casefold()
            if isinstance(value, int)
        )
        mentioned_subagents = {
            name for name in expected_subagents if name in serialized
        }
        missing_phrases = [
            phrase for phrase in required_phrases if phrase not in answer
        ]
        missing_subagents = sorted(set(expected_subagents) - mentioned_subagents)
        forbidden_hits = [
            phrase for phrase in forbidden_phrases if phrase in serialized
        ]
        errors = run.get("errors") or []
        sample_reports.append(
            {
                "query": run.get("query"),
                "has_answer": bool(answer.strip()),
                "evidence_citation_count": len(_EVIDENCE_RE.findall(answer)),
                "unique_evidence_count": len(evidence_ids),
                "tool_event_count": tool_events,
                "subagent_event_count": subagent_events,
                "expected_subagents_seen": sorted(mentioned_subagents),
                "expected_subagents_missing": missing_subagents,
                "required_phrases_missing": missing_phrases,
                "forbidden_phrase_hits": forbidden_hits,
                "error_count": len(errors) if isinstance(errors, list) else 0,
                "ok": (
                    bool(answer.strip())
                    and not missing_phrases
                    and not forbidden_hits
                    and not errors
                    and not missing_subagents
                ),
            }
        )
    return {
        "generated_by": "scripts/evaluate_nexent_transcripts.py",
        "agent_name": transcript.get("agent_name"),
        "tenant_id": transcript.get("tenant_id"),
        "run_count": len(sample_reports),
        "all_runs_ok": all(item["ok"] for item in sample_reports),
        "aggregate": {
            "evidence_citation_count": sum(
                item["evidence_citation_count"] for item in sample_reports
            ),
            "unique_evidence_count": sum(
                item["unique_evidence_count"] for item in sample_reports
            ),
            "tool_event_count": sum(
                item["tool_event_count"] for item in sample_reports
            ),
            "subagent_event_count": sum(
                item["subagent_event_count"] for item in sample_reports
            ),
            "error_count": sum(item["error_count"] for item in sample_reports),
        },
        "runs": sample_reports,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--run",
        action="append",
        default=[],
        metavar="NAME=TRANSCRIPT.json",
        help="Evaluation arm; repeat for single_agent and three_agent.",
    )
    parser.add_argument("--required-phrase", action="append", default=[])
    parser.add_argument("--forbidden-phrase", action="append", default=[])
    parser.add_argument("--expect-subagent", action="append", default=[])
    parser.add_argument(
        "--expect-subagent-in",
        action="append",
        default=[],
        metavar="ARM=NAME",
        help=(
            "Require a subagent only in one arm. Repeat for multiple names; "
            "the single-Agent arm can therefore require none."
        ),
    )
    parser.add_argument("--out", type=Path)
    return parser


def _parse_run(value: str) -> tuple[str, Path]:
    if "=" not in value:
        raise ValueError(f"--run must be NAME=TRANSCRIPT.json, got {value!r}")
    name, path = value.split("=", 1)
    if not name.strip() or not path.strip():
        raise ValueError(f"--run must be NAME=TRANSCRIPT.json, got {value!r}")
    return name.strip(), Path(path).expanduser().resolve()


def _parse_arm_assignment(value: str) -> tuple[str, str]:
    if "=" not in value:
        raise ValueError(f"expected ARM=NAME, got {value!r}")
    arm, name = value.split("=", 1)
    if not arm.strip() or not name.strip():
        raise ValueError(f"expected ARM=NAME, got {value!r}")
    return arm.strip(), name.strip()


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.run:
        raise SystemExit("at least one --run NAME=TRANSCRIPT.json is required")
    forbidden = args.forbidden_phrase or ["<|DSML|", "<｜DSML｜"]
    report: dict[str, Any] = {"arms": {}}
    arm_subagents: dict[str, list[str]] = {}
    for raw_assignment in args.expect_subagent_in:
        arm, name = _parse_arm_assignment(raw_assignment)
        arm_subagents.setdefault(arm, []).append(name)
    for raw_run in args.run:
        name, path = _parse_run(raw_run)
        transcript = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(transcript, dict):
            raise SystemExit(f"{path} must contain a JSON object")
        report["arms"][name] = {
            "transcript": str(path),
            **evaluate_transcript(
                transcript,
                required_phrases=args.required_phrase,
                forbidden_phrases=forbidden,
                expected_subagents=[
                    *args.expect_subagent,
                    *arm_subagents.get(name, []),
                ],
            ),
        }
    if len(report["arms"]) == 2:
        first_name, first = next(iter(report["arms"].items()))
        second_name, second = list(report["arms"].items())[1]
        first_queries = [str(item.get("query") or "") for item in first["runs"]]
        second_queries = [str(item.get("query") or "") for item in second["runs"]]
        report["comparison"] = {
            "arms": [first_name, second_name],
            "matched_queries": first_queries == second_queries,
            "evidence_citation_delta": second["aggregate"]["evidence_citation_count"]
            - first["aggregate"]["evidence_citation_count"],
            "tool_event_delta": second["aggregate"]["tool_event_count"]
            - first["aggregate"]["tool_event_count"],
            "subagent_event_delta": second["aggregate"]["subagent_event_count"]
            - first["aggregate"]["subagent_event_count"],
        }
    text = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text, encoding="utf-8")
    print(text, end="")
    matched = report.get("comparison", {}).get("matched_queries", True)
    return (
        0
        if matched and all(arm["all_runs_ok"] for arm in report["arms"].values())
        else 1
    )


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["build_parser", "evaluate_transcript", "main"]
