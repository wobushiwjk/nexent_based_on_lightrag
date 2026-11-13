"""Command-line entry point for the read-only ontology governance MCP server."""

from __future__ import annotations

import argparse
import logging
from dataclasses import replace
from pathlib import Path

from governance_mcp_server.bootstrap import bootstrap_ontology_governance
from governance_mcp_server.config import GovernanceServerConfig
from governance_mcp_server.server import build_server
from governance_mcp_server.service import GovernanceService

logger = logging.getLogger(__name__)

_LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lightrag-ontology-governance-mcp",
        description="Serve immutable ontology governance state over MCP.",
    )
    parser.add_argument("--root", dest="governance_root")
    parser.add_argument("--ontology", dest="ontology_path")
    parser.add_argument(
        "--bootstrap-if-empty",
        action="store_true",
        help=(
            "Install and activate the canonical ontology only when the "
            "governance root has no versions and no active pointer."
        ),
    )
    parser.add_argument(
        "--transport",
        choices=("stdio", "sse", "streamable-http"),
        default="stdio",
    )
    parser.add_argument("--host")
    parser.add_argument("--port", type=int)
    parser.add_argument("--log-level", choices=sorted(_LOG_LEVELS))
    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        config = GovernanceServerConfig.from_env()
        overrides: dict[str, object] = {}
        if args.governance_root:
            overrides["governance_root"] = (
                Path(args.governance_root).expanduser().resolve()
            )
        if args.ontology_path:
            overrides["ontology_path"] = Path(args.ontology_path).expanduser().resolve()
        if args.host:
            overrides["host"] = args.host
        if args.port is not None:
            if args.port < 1 or args.port > 65535:
                raise ValueError("--port must be between 1 and 65535")
            overrides["port"] = args.port
        if args.log_level:
            overrides["log_level"] = args.log_level
        config = replace(config, **overrides)
        level = config.log_level if config.log_level in _LOG_LEVELS else "INFO"
        logging.basicConfig(
            level=getattr(logging, level),
            format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        )
        if args.bootstrap_if_empty:
            installed = bootstrap_ontology_governance(
                governance_root=config.governance_root,
                ontology_path=config.ontology_path,
            )
            if installed is not None:
                logger.info(
                    "bootstrapped ontology version %s",
                    installed.ontology_version,
                )
        service = GovernanceService(config.governance_root)
        server = build_server(
            service,
            name=config.server_name,
            log_level=level,
            host=config.host,
            port=config.port,
        )
    except Exception as exc:
        logger.error("cannot start ontology governance MCP server: %s", exc)
        raise SystemExit(2) from exc

    logger.info(
        "serving %s over %s from %s",
        config.server_name,
        args.transport,
        config.governance_root,
    )
    server.run(transport=args.transport)


if __name__ == "__main__":
    main()


__all__ = ["build_parser", "main"]
