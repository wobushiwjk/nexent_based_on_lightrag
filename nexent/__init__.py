"""Nexent orchestration layer for the industrial fault domain.

This package holds the *deployment* artifacts — version lock, configuration,
import scripts, agent prompt, demo and rollback notes — that register the
repository's MCP tools, Skills and knowledge base inside a running Nexent
instance (W5).

Nothing in here is imported by ``lightrag``, ``mcp_server`` or ``skills``:
the repository is the source of truth and Nexent is a consumer of it. That
separation is what keeps the domain services usable without Nexent and keeps
this package free of runtime imports from the rest of the repo.
"""
