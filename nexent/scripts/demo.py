"""Drive the imported agent through Nexent's northbound chat API.

The demo answers the W5 acceptance question — "the agent can call the W2
tools and the W4 Skills and show evidence" — with a recorded transcript. It
uses the northbound API rather than the web UI because that is the integration
surface an external system would use, and because it proves the *published*
agent is reachable, not just the editable draft.

Complete mode (the northbound default) emits exactly one ``final_answer``
event carrying the whole answer, so the transcript is deterministic. Tool
cards and intermediate steps still arrive as other event types and are
counted per type, which is what shows the MCP tools ran.

Run from the repository root::

    .venv/Scripts/python.exe -m nexent.scripts.demo \
        --query "P-101A 冷却水泵出口压力不足，可能的原因有哪些？请给出证据出处。"
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

from nexent.scripts.env_file import load_nexent_env
from nexent.scripts.nexent_client import (
    DEFAULT_CONFIG_URL,
    DEFAULT_NORTHBOUND_URL,
    NexentClient,
    NexentError,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_AGENT_NAME = "industrial_fault_agent"
DEFAULT_QUERY = (
    "P-101A 冷却水泵出口压力不足，可能的原因有哪些？"
    "请逐条给出证据出处，并说明故障树中的位置。"
)
DEFAULT_GOVERNANCE_QUERY = "当前工业故障本体是哪个版本？请说明安装时间和类、关系数量。"
DEFAULT_OUT_DIR = REPO_ROOT / "nexent" / "build"
DEFAULT_EMAIL = "suadmin@nexent.com"
DEFAULT_PASSWORD = "Nexent@123"


class DemoError(RuntimeError):
    """The chat run produced no answer or ended in an error event."""


def _answer_text(event: Mapping[str, Any]) -> str:
    """Return the text carried by one answer event, of whatever shape."""
    content = event.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        for key in ("content", "text", "answer"):
            value = content.get(key)
            if isinstance(value, str):
                return value
        return json.dumps(content, ensure_ascii=False)
    return "" if content is None else str(content)


class AnswerStream:
    """Reassemble the final answer from Nexent's answer events.

    Nexent v2.8.0 ignores ``final_answer_mode`` and always streams the answer
    as ``answer_phase: "delta"`` fragments, followed by one ``complete``
    marker whose content is normally empty. A multi-step run can emit an
    answer per step, so a step change resets the buffer and the last step's
    answer wins. Newer builds send a single ``complete`` event carrying the
    whole text; a non-empty one supersedes the fragments.
    """

    def __init__(self) -> None:
        self._step: Any = None
        self._fragments: list[str] = []
        self._complete: str | None = None

    def feed(self, event: Mapping[str, Any]) -> None:
        step = event.get("step_id")
        if step is not None and step != self._step:
            self._step = step
            self._fragments = []
            self._complete = None
        text = _answer_text(event)
        if event.get("answer_phase") == "complete":
            if text:
                self._complete = text
        elif text:
            self._fragments.append(text)

    def text(self) -> str:
        if self._complete is not None:
            return self._complete
        return "".join(self._fragments)


@dataclass(frozen=True)
class DemoOptions:
    """Parameters of one demo run."""

    config_url: str = DEFAULT_CONFIG_URL
    northbound_url: str = DEFAULT_NORTHBOUND_URL
    email: str = DEFAULT_EMAIL
    password: str = DEFAULT_PASSWORD
    agent_name: str = DEFAULT_AGENT_NAME
    queries: tuple[str, ...] = (DEFAULT_QUERY, DEFAULT_GOVERNANCE_QUERY)
    timeout: float = 600.0
    out_dir: Path = DEFAULT_OUT_DIR
    output: Path | None = None


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def run_chat(
    northbound: NexentClient,
    *,
    access_key: str,
    agent_name: str,
    query: str,
) -> dict[str, Any]:
    """Run one chat request and collect its streamed events."""
    event_counts: dict[str, int] = {}
    answer = AnswerStream()
    errors: list[Any] = []
    conversation_id: Any = None

    events: Iterator[dict[str, Any]] = northbound.stream_events(
        "POST",
        "/nb/v1/chat/run",
        json_body={
            "agent_name": agent_name,
            "query": query,
            "final_answer_mode": "complete",
        },
        token=access_key,
    )
    for event in events:
        event_type = str(event.get("type") or "unknown")
        event_counts[event_type] = event_counts.get(event_type, 0) + 1
        content = event.get("content")
        if event_type == "conversation_created" and isinstance(content, dict):
            conversation_id = content.get("conversation_id")
        elif event_type == "final_answer":
            answer.feed(event)
        elif event_type == "error":
            errors.append(content)

    final_answer = answer.text()
    result: dict[str, Any] = {
        "query": query,
        "conversation_id": conversation_id,
        "events": event_counts,
        "errors": errors,
        "final_answer": final_answer or None,
    }
    if not final_answer:
        raise DemoError(
            f"no final_answer event for query {query!r}; "
            f"errors: {errors or 'none'}; events: {event_counts}"
        )
    return result


def run_demo(
    options: DemoOptions,
    *,
    client: NexentClient | None = None,
    northbound: NexentClient | None = None,
    out: Callable[[str], None] = print,
) -> dict[str, Any]:
    """Sign in, mint an API key, run every query, and write the transcript."""
    management = client or NexentClient(options.config_url, timeout=options.timeout)
    management.signin(options.email, options.password)
    user = management.current_user()
    user_id = user.get("user_id")
    if not user_id:
        raise DemoError("current_user_info returned no user_id")
    access_key = management.ensure_api_key(str(user_id))

    north = northbound or NexentClient(options.northbound_url, timeout=options.timeout)
    transcript: dict[str, Any] = {
        "generated_by": "nexent/scripts/demo.py",
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "agent_name": options.agent_name,
        "tenant_id": user.get("tenant_id"),
        "northbound_url": options.northbound_url,
        "runs": [],
    }
    for query in options.queries:
        out(f"[query] {query}")
        result = run_chat(
            north,
            access_key=access_key,
            agent_name=options.agent_name,
            query=query,
        )
        transcript["runs"].append(result)
        out(result["final_answer"] or "")

    options.out_dir.mkdir(parents=True, exist_ok=True)
    target = options.output or (options.out_dir / f"demo-{_utc_stamp()}.json")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(transcript, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    transcript["transcript_path"] = str(target)
    return transcript


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Ask the imported agent a question through Nexent and save the transcript.",
    )
    parser.add_argument(
        "--config-url",
        default=os.environ.get("NEXENT_CONFIG_URL", DEFAULT_CONFIG_URL),
    )
    parser.add_argument(
        "--northbound-url",
        default=os.environ.get("NEXENT_NORTHBOUND_URL", DEFAULT_NORTHBOUND_URL),
    )
    parser.add_argument(
        "--email", default=os.environ.get("NEXENT_EMAIL", DEFAULT_EMAIL)
    )
    parser.add_argument(
        "--password", default=os.environ.get("NEXENT_PASSWORD", DEFAULT_PASSWORD)
    )
    parser.add_argument("--agent-name", default=DEFAULT_AGENT_NAME)
    parser.add_argument(
        "--query",
        action="append",
        dest="queries",
        help="Question to ask; repeat for several questions.",
    )
    parser.add_argument("--timeout", type=float, default=600.0)
    parser.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR))
    parser.add_argument(
        "--output",
        help="Exact transcript path; useful for matched W9 ablation runs.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8")
    load_nexent_env()
    args = build_parser().parse_args(argv)
    options = DemoOptions(
        config_url=args.config_url,
        northbound_url=args.northbound_url,
        email=args.email,
        password=args.password,
        agent_name=args.agent_name,
        queries=tuple(args.queries or (DEFAULT_QUERY, DEFAULT_GOVERNANCE_QUERY)),
        timeout=args.timeout,
        out_dir=Path(args.out_dir).expanduser().resolve(),
        output=(Path(args.output).expanduser().resolve() if args.output else None),
    )
    try:
        transcript = run_demo(options)
    except (DemoError, NexentError) as exc:
        print(f"[error] {exc}", file=sys.stderr)
        return 1
    print(json.dumps({"transcript": transcript["transcript_path"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
