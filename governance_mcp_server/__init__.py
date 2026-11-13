"""Read-only MCP surface for ontology governance."""

from governance_mcp_server.bootstrap import bootstrap_ontology_governance
from governance_mcp_server.server import build_server
from governance_mcp_server.service import GovernanceService

__all__ = [
    "GovernanceService",
    "bootstrap_ontology_governance",
    "build_server",
]
