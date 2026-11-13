"""Command-line entry point for the read-only domain MCP server."""

from __future__ import annotations

import argparse
import logging
from dataclasses import replace
from pathlib import Path

from mcp_server.config import ServerConfig
from mcp_server.repository import DomainRepository, DomainRepositoryError
from mcp_server.server import build_server
from mcp_server.service import DomainService
from mcp_server.storage_repository import DomainStorageRepository

logger = logging.getLogger(__name__)

_LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lightrag-domain-mcp",
        description="Serve the read-only industrial fault-tree domain over MCP.",
    )
    parser.add_argument(
        "--pack",
        dest="pack_dir",
        help="Domain pack directory containing ontology.yml (default: env or repo).",
    )
    parser.add_argument(
        "--bundle",
        action="append",
        dest="bundles",
        help=(
            "Bundle JSON path; repeat for multiple tenants. Overrides "
            "MCP_DOMAIN_BUNDLES."
        ),
    )
    parser.add_argument(
        "--storage-dir",
        dest="storage_dir",
        help=(
            "Importer output directory with tenant runtime bundles and "
            "evidence.sqlite3. Overrides MCP_DOMAIN_STORAGE_DIR."
        ),
    )
    parser.add_argument(
        "--default-tenant",
        help="Tenant used when a tool receives an empty tenant_id.",
    )
    parser.add_argument(
        "--governance-root",
        help=(
            "Ontology governance root; defaults to <storage-dir>/governance "
            "when present."
        ),
    )
    parser.add_argument(
        "--transport",
        choices=("stdio", "sse", "streamable-http"),
        default="stdio",
        help="MCP transport (default: stdio).",
    )
    parser.add_argument("--host", help="Bind host for HTTP transports.")
    parser.add_argument("--port", type=int, help="Bind port for HTTP transports.")
    parser.add_argument(
        "--log-level",
        choices=sorted(_LOG_LEVELS),
        help="Server log level.",
    )
    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.bundles and args.storage_dir:
        parser.error("--bundle and --storage-dir are mutually exclusive")
    try:
        config = ServerConfig.from_env()
        overrides: dict[str, object] = {}
        if args.pack_dir:
            overrides["pack_dir"] = Path(args.pack_dir).expanduser().resolve()
        if args.bundles:
            overrides["bundle_paths"] = tuple(
                Path(item).expanduser().resolve() for item in args.bundles
            )
        if args.storage_dir:
            overrides["storage_dir"] = Path(args.storage_dir).expanduser().resolve()
        if args.default_tenant is not None:
            overrides["default_tenant"] = args.default_tenant.strip() or None
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
        if config.storage_dir is not None:
            repository = DomainStorageRepository.from_storage_dir(
                config.pack_dir,
                config.storage_dir,
                default_tenant=config.default_tenant,
                governance_root=(
                    Path(args.governance_root).expanduser().resolve()
                    if args.governance_root
                    else None
                ),
            )
        else:
            repository = DomainRepository.from_paths(
                config.pack_dir,
                config.bundle_paths,
                default_tenant=config.default_tenant,
            )
        service = DomainService(repository)  # type: ignore[arg-type]
        server = build_server(
            service,
            name=config.server_name,
            log_level=level,
            host=config.host,
            port=config.port,
        )
    except DomainRepositoryError as exc:
        logger.error("cannot load domain repository: %s", exc)
        for issue in exc.issues:
            logger.error(
                "%s [%s]: %s",
                issue.severity.upper(),
                issue.target_id or "-",
                issue.message,
            )
        raise SystemExit(2) from exc
    except Exception as exc:
        logger.error("cannot start domain MCP server: %s", exc)
        raise SystemExit(2) from exc

    logger.info(
        "serving %s over %s (tenants: %s)",
        config.server_name,
        args.transport,
        ", ".join(repository.tenants),
    )
    try:
        server.run(transport=args.transport)
    finally:
        close = getattr(repository, "close", None)
        if callable(close):
            close()


if __name__ == "__main__":
    main()
