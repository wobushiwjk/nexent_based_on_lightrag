"""Command-line entry point for the upload ingestion MCP server."""

from __future__ import annotations

import argparse
import logging
from dataclasses import replace
from pathlib import Path

from ingestion_mcp_server.config import IngestionServerConfig
from ingestion_mcp_server.server import build_server
from ingestion_mcp_server.service import IngestionToolService
from lightrag.fault_tree.ingestion import (
    DocumentIngestionService,
    build_extractor_from_env,
)
from lightrag.fault_tree.multimodal import OpenAICompatibleVlm
from nexent.scripts.env_file import load_nexent_env

logger = logging.getLogger(__name__)

_LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lightrag-fault-ingestion-mcp",
        description="Expose Nexent-uploaded documents to the fault domain.",
    )
    parser.add_argument("--pack", dest="pack_dir")
    parser.add_argument("--storage-dir")
    parser.add_argument(
        "--upload-dir",
        action="append",
        dest="upload_dirs",
        help="Allowed upload root; repeat for multiple roots.",
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
    load_nexent_env()
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        config = IngestionServerConfig.from_env()
        overrides: dict[str, object] = {}
        if args.pack_dir:
            overrides["pack_dir"] = Path(args.pack_dir).expanduser().resolve()
        if args.storage_dir:
            overrides["storage_dir"] = Path(args.storage_dir).expanduser().resolve()
        if args.upload_dirs:
            overrides["upload_dirs"] = tuple(
                Path(item).expanduser().resolve() for item in args.upload_dirs
            )
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
        extractor = build_extractor_from_env()
        vlm = (
            OpenAICompatibleVlm(
                config.vlm_endpoint,
                config.vlm_model,
                api_key=config.vlm_api_key,
            )
            if config.vlm_endpoint and config.vlm_model
            else None
        )
        service = DocumentIngestionService(
            pack_dir=config.pack_dir,
            storage_dir=config.storage_dir,
            upload_dirs=config.upload_dirs,
            extractor=extractor,
            vlm=vlm,
        )
        server = build_server(
            IngestionToolService(service),
            name=config.server_name,
            log_level=level,
            host=config.host,
            port=config.port,
        )
    except Exception as exc:
        logger.error("cannot start ingestion MCP server: %s", exc)
        raise SystemExit(2) from exc

    logger.info(
        "serving %s over %s (uploads: %s)",
        config.server_name,
        args.transport,
        ", ".join(str(path) for path in config.upload_dirs),
    )
    server.run(transport=args.transport)


if __name__ == "__main__":
    main()


__all__ = ["build_parser", "main"]
