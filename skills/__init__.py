"""Deterministic reference implementations of the Nexent skills.

Each skill under this package consumes a JSON request and reads domain data
exclusively through :class:`skills.runtime.ToolClient`, whose production
implementation is an MCP session.  The package deliberately imports neither
``lightrag`` nor ``mcp_server`` so that dependency is enforced by code shape,
not only by convention.
"""

from __future__ import annotations

from . import runtime

__all__ = ["runtime"]
