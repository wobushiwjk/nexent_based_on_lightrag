#!/usr/bin/env python
"""
执行入口。
启动真实 MCP stdio 子进程，把 MCP 会话适配成 Skill 能用的客户端，校验服务端暴露的工具集合，并输出统一响应。
支持静态 bundle 或 storage-backed 数据；退出码区分业务失败和执行/传输失败。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from skills.evidence_verification.handler import run as run_evidence
from skills.fault_diagnosis.handler import run as run_diagnosis
from skills.ontology_change_draft.handler import run as run_ontology
from skills.runtime import READ_ONLY_TOOLS, SkillResponse

DEFAULT_PACK = ROOT / "domain_packs" / "industrial_fault"
DEFAULT_BUNDLE = DEFAULT_PACK / "seeds" / "demo_bundle.json"
SKILL_RUNNERS = {
    "fault_diagnosis": run_diagnosis,
    "evidence_verification": run_evidence,
    "ontology_change_draft": run_ontology,
}


class MCPToolClient:
    """Adapt one MCP client session to the skill ToolClient protocol."""

    def __init__(self, session: ClientSession) -> None:
        self.session = session

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        result = await asyncio.wait_for(
            self.session.call_tool(name, arguments),
            timeout=30,
        )
        structured = getattr(result, "structuredContent", None)
        if isinstance(structured, dict):
            return structured
        return json.loads(result.content[0].text)


async def run(
    *,
    skill: str,
    payload: dict[str, Any],
    pack: Path,
    bundle: Path,
    storage_dir: Path | None,
) -> tuple[SkillResponse, int]:
    env = os.environ.copy()
    env["MCP_DOMAIN_PACK"] = str(pack)
    if storage_dir is not None:
        env["MCP_DOMAIN_STORAGE_DIR"] = str(storage_dir)
        env.pop("MCP_DOMAIN_BUNDLES", None)
    else:
        env["MCP_DOMAIN_BUNDLES"] = str(bundle)
        env.pop("MCP_DOMAIN_STORAGE_DIR", None)
    env["MCP_LOG_LEVEL"] = "WARNING"
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "mcp_server"],
        cwd=str(ROOT),
        env=env,
    )
    runner = SKILL_RUNNERS[skill]
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await asyncio.wait_for(session.initialize(), timeout=30)
            listed = await asyncio.wait_for(session.list_tools(), timeout=30)
            names = {tool.name for tool in listed.tools}
            if names != READ_ONLY_TOOLS:
                raise RuntimeError(
                    "MCP tool set mismatch: "
                    f"missing={sorted(READ_ONLY_TOOLS - names)}, "
                    f"extra={sorted(names - READ_ONLY_TOOLS)}"
                )
            response = await runner(payload, MCPToolClient(session))
    return response, 0 if response.ok else 1


def _load_payload(path: str) -> dict[str, Any]:
    if path == "-":
        text = sys.stdin.read()
    else:
        text = Path(path).expanduser().read_text(encoding="utf-8")
    payload = json.loads(text)
    if not isinstance(payload, dict):
        raise ValueError("skill input must be a JSON object")
    return payload


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--skill",
        required=True,
        choices=sorted(SKILL_RUNNERS),
        help="Skill to execute.",
    )
    parser.add_argument(
        "--input",
        required=True,
        help="JSON request file, or - to read it from stdin.",
    )
    parser.add_argument("--pack", default=str(DEFAULT_PACK))
    parser.add_argument("--bundle", default=str(DEFAULT_BUNDLE))
    parser.add_argument(
        "--storage-dir",
        help=(
            "Serve the importer output directory instead of --bundle; "
            "the skill sees the same read-only tools either way."
        ),
    )
    args = parser.parse_args(argv)

    try:
        payload = _load_payload(args.input)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"cannot read skill input: {exc}", file=sys.stderr)
        return 2

    try:
        response, code = asyncio.run(
            run(
                skill=args.skill,
                payload=payload,
                pack=Path(args.pack).expanduser().resolve(),
                bundle=Path(args.bundle).expanduser().resolve(),
                storage_dir=(
                    Path(args.storage_dir).expanduser().resolve()
                    if args.storage_dir
                    else None
                ),
            )
        )
    except Exception as exc:  # transport and startup failures
        print(f"skill run failed before a response: {exc}", file=sys.stderr)
        return 2

    print(
        json.dumps(
            response.model_dump(mode="json"),
            ensure_ascii=False,
            indent=2,
        )
    )
    return code


if __name__ == "__main__":
    raise SystemExit(main())
