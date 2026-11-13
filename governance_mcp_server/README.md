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
