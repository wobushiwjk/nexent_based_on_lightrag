"""Structured errors for the read-only domain MCP service.

Business errors travel inside the tool result envelope so an agent can inspect
``error.code`` and recover.  Only transport-level failures (unknown tool,
argument-schema mismatch) are reported by the MCP runtime itself.
"""

from __future__ import annotations

from typing import Any, Literal

ErrorCode = Literal[
    "invalid_argument",
    "not_found",
    "ambiguous_selector",
    "unsupported",
    "internal_error",
]

INVALID_ARGUMENT: ErrorCode = "invalid_argument"
NOT_FOUND: ErrorCode = "not_found"
AMBIGUOUS_SELECTOR: ErrorCode = "ambiguous_selector"
UNSUPPORTED: ErrorCode = "unsupported"
INTERNAL_ERROR: ErrorCode = "internal_error"


class DomainError(Exception):
    """A business error that is safe to return to an MCP client."""

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


class DomainRepositoryError(Exception):
    """Raised at startup when a domain bundle cannot be served."""

    def __init__(self, message: str, issues: list[Any] | None = None) -> None:
        super().__init__(message)
        self.issues = list(issues or [])
