#!/usr/bin/env python
"""Call every read-only domain MCP tool over a real stdio session.

The script is intentionally independent of Nexent: it starts the server as a
subprocess, performs the MCP handshake, calls each tool, checks the common
envelope, and verifies that a cross-tenant lookup is rejected.  It also hashes
the domain pack before and after to prove the tools did not write to it.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PACK = REPO_ROOT / "domain_packs" / "industrial_fault"
DEFAULT_BUNDLE = DEFAULT_PACK / "seeds" / "demo_bundle.json"
TOOL_NAMES = {
    "get_ontology",
    "get_equipment",
    "search_faults",
    "get_fault_tree",
    "trace_evidence",
    "validate_fault_tree",
}


def _digest(root: Path) -> dict[str, str]:
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _payload(result: Any) -> dict[str, Any]:
    structured = getattr(result, "structuredContent", None)
    if isinstance(structured, dict):
        return structured
    return json.loads(result.content[0].text)


def _summary(name: str, data: dict[str, Any]) -> str:
    if name == "get_ontology":
        counts = data["counts"]
        return (
            f"{counts['classes']} classes, {counts['relations']} relations, "
            f"{counts['constraints']} constraints"
        )
    if name == "get_equipment":
        return f"{data['equipment']['uid']} ({data['equipment']['equipment_tag']})"
    if name == "search_faults":
        first = data["results"][0]["uid"] if data["results"] else "-"
        return f"{data['count']} matches, first={first}"
    if name == "get_fault_tree":
        counts = data["counts"]
        return (
            f"{data['draft']['draft_id']} with {counts['nodes']} nodes, "
            f"{counts['edges']} edges, {counts['gates']} gates"
        )
    if name == "trace_evidence":
        return f"{data['count']} evidence fragments"
    if name == "validate_fault_tree":
        return (
            f"valid={data['valid']}, errors={data['error_count']}, "
            f"warnings={data['warning_count']}"
        )
    return "ok"


async def run(
    *,
    pack: Path,
    bundle: Path,
    storage_dir: Path | None,
    tenant: str,
    show_json: bool,
) -> int:
    before = _digest(pack)
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
        cwd=str(REPO_ROOT),
        env=env,
    )
    calls = [
        ("get_ontology", {}),
        ("get_equipment", {"tenant_id": tenant, "equipment_ref": "P-101A"}),
        ("search_faults", {"tenant_id": tenant, "query": "出口压力不足"}),
        (
            "get_fault_tree",
            {"tenant_id": tenant, "top_event_uid": "FAULT-LOW-PRESSURE"},
        ),
        (
            "trace_evidence",
            {
                "tenant_id": tenant,
                "target_type": "rule",
                "target_id": "RULE-CAVITATION-AND",
            },
        ),
        (
            "validate_fault_tree",
            {
                "tenant_id": tenant,
                "draft_id": "TREE-P101A-LOW-PRESSURE-v1",
            },
        ),
    ]

    failures: list[str] = []
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await asyncio.wait_for(session.initialize(), timeout=30)
            listed = await asyncio.wait_for(session.list_tools(), timeout=30)
            names = {tool.name for tool in listed.tools}
            if names != TOOL_NAMES:
                failures.append(
                    f"tool set mismatch: missing={sorted(TOOL_NAMES - names)}, "
                    f"extra={sorted(names - TOOL_NAMES)}"
                )
            for tool in listed.tools:
                if tool.annotations is None or not tool.annotations.readOnlyHint:
                    failures.append(f"{tool.name} is not marked read-only")

            for name, arguments in calls:
                result = await asyncio.wait_for(
                    session.call_tool(name, arguments),
                    timeout=30,
                )
                payload = _payload(result)
                if result.isError or not payload.get("ok"):
                    failures.append(
                        f"{name} failed: {json.dumps(payload, ensure_ascii=False)}"
                    )
                    continue
                print(f"[ok] {name}: {_summary(name, payload['data'])}")
                if show_json:
                    print(json.dumps(payload, ensure_ascii=False, indent=2))

            cross = await asyncio.wait_for(
                session.call_tool(
                    "get_equipment",
                    {"tenant_id": "tenant-not-loaded", "equipment_ref": "P-101A"},
                ),
                timeout=30,
            )
            cross_payload = _payload(cross)
            if (
                cross.isError
                or cross_payload.get("ok")
                or cross_payload.get("error", {}).get("code") != "not_found"
            ):
                failures.append("cross-tenant lookup was not rejected with not_found")
            else:
                print("[ok] cross-tenant lookup rejected with not_found")

    after = _digest(pack)
    if after != before:
        changed = sorted(
            {
                key
                for key in set(before) | set(after)
                if before.get(key) != after.get(key)
            }
        )
        failures.append(f"domain pack changed during the run: {changed}")
    else:
        print("[ok] domain pack unchanged (read-only)")

    if failures:
        for failure in failures:
            print(f"[fail] {failure}", file=sys.stderr)
        return 1
    print("All read-only MCP tool checks passed.")
    return 0


def main(argv: list[str] | None = None) -> int:
    # Git Bash and Windows terminals may default to a legacy code page.  MCP
    # itself is UTF-8; make redirected output keep the same encoding so the
    # Chinese quotes in the JSON examples survive `--json > file`.
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pack", default=str(DEFAULT_PACK))
    parser.add_argument("--bundle", default=str(DEFAULT_BUNDLE))
    parser.add_argument(
        "--storage-dir",
        help=(
            "Serve the importer output directory (runtime bundles plus "
            "evidence.sqlite3) instead of --bundle."
        ),
    )
    parser.add_argument("--tenant", default="tenant-demo")
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print the full response envelope for every tool.",
    )
    args = parser.parse_args(argv)
    storage_dir = (
        Path(args.storage_dir).expanduser().resolve()
        if args.storage_dir
        else None
    )
    return asyncio.run(
        run(
            pack=Path(args.pack).expanduser().resolve(),
            bundle=Path(args.bundle).expanduser().resolve(),
            storage_dir=storage_dir,
            tenant=args.tenant,
            show_json=args.json,
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
