# Ontology Governance MCP

This server exposes the immutable ontology governance store through a separate
read-only MCP service. The existing `industrial-fault-domain` MCP server and
the three domain Skills are unchanged.

## Tools

- `get_ontology_governance_status`
- `list_ontology_versions`
- `get_ontology_version`
- `list_ontology_changes`
- `get_ontology_change`
- `create_ontology_change_draft`
- `submit_ontology_change_review`
- `review_ontology_change`
- `analyze_ontology_change_impact`
- `publish_ontology_change`
- `rollback_ontology_publication`
- `list_tenant_snapshot_history`

`create_ontology_change_draft` writes an immutable draft and its source excerpt
snapshot. It validates verbatim evidence quotes and conflicts with the active
ontology, but never approves, migrates, activates, or publishes a version.

The review and publication tools require explicit `confirm=true`. Publication
first builds all migrated tenant snapshots, then atomically switches the
ontology and tenant pointers. The previous ontology version and tenant runs are
retained for rollback and historical inspection.

Publication also projects the evolved ontology into a versioned set of JSON
Schemas. New concrete `Asset`/`Equipment` classes appear in
`entity.schema.json` (`$defs`, `oneOf`, and `discriminator.mapping`) and in the
entity union used by `domain_bundle.schema.json`. The artifact hashes are stored
in the immutable version manifest and verified whenever the version is loaded.
`get_ontology_version(..., include_schema=true)` returns the actual Schema JSON.

Two unresolved changes with the same base and proposed versions cannot coexist
after this release: creating a second draft is rejected, and approving one is
blocked while another unresolved proposal targets the same version. Rejected
changes remain in the audit timeline and no longer block a replacement draft.

Impact analysis and snapshot history are read-only. The domain MCP refreshes its
served ontology when the governance active pointer changes, so published or
rolled-back semantics become visible without restarting the domain MCP.

## Start locally for Nexent

```bash
.venv/Scripts/python.exe -m governance_mcp_server \
  --bootstrap-if-empty \
  --transport streamable-http \
  --host 0.0.0.0 \
  --port 8766
```

Bootstrap is limited to a completely empty governance root. An existing store
with versions but no active pointer is rejected instead of silently selecting
a version.

In Docker Desktop, Nexent reaches this server at:

```text
http://host.docker.internal:8766/mcp
```

Register it through `nexent/config/governance_mcp.json` and rerun the Nexent
import flow. Users then ask the main Agent questions such as:

- “当前本体是哪个版本？”
- “有哪些历史本体版本？”
- “v0.1 包含哪些类和关系？”
- “目前有没有本体变更记录？”
- “根据这段标准节选生成 v0.2 本体变更草案：<粘贴原文>”
- “读取 v0.2 的 entity Schema，确认 Sensor 是否进入 `$defs` 和 `oneOf`。”

## Isolated acceptance

Run the complete conflict, review, impact, dry-run, publication, Schema, tenant
snapshot and rollback flow without touching active storage:

```bash
.venv/Scripts/python.exe scripts/w8_acceptance.py
```

The report proves that the superseded same-version draft is rejected before a
source-supported three-operation revision, `CHG-0003`, is published and rolled
back. The old and new tenant snapshots both remain available.
