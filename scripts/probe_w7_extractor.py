#!/usr/bin/env python
"""Probe the configured W7 student through the same path used by ingestion."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from lightrag.fault_tree.ingestion import (  # noqa: E402
    IngestionError,
    OpenAICompatibleStudentExtractor,
    build_extractor_from_env,
)
from nexent.scripts.env_file import load_nexent_env  # noqa: E402

SAMPLE = (
    "P-101A 出口压力不足。检查发现入口过滤器堵塞，清理过滤器后压力恢复。"
    "手册说明入口过滤器堵塞会降低泵入口流量，并进一步造成出口压力不足。"
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--endpoint",
        default=os.environ.get("W7_STUDENT_ENDPOINT", ""),
    )
    parser.add_argument(
        "--model",
        default=os.environ.get("W7_STUDENT_MODEL", ""),
    )
    parser.add_argument(
        "--api-key",
        default=os.environ.get("W7_STUDENT_API_KEY", ""),
    )
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--text", default=SAMPLE)
    return parser


def main(argv: list[str] | None = None) -> int:
    load_nexent_env()
    args = build_parser().parse_args(argv)
    try:
        extractor = (
            build_extractor_from_env()
            if not args.endpoint and not args.model
            else OpenAICompatibleStudentExtractor(
                args.endpoint,
                args.model,
                api_key=args.api_key,
                timeout=args.timeout,
            )
        )
        if extractor is None:
            raise IngestionError(
                "extractor_not_configured",
                "set W7_STUDENT_ENDPOINT and W7_STUDENT_MODEL",
            )
        batch = extractor.extract(
            chunk_id="probe:1",
            asset_id="FILE-PROBE",
            chunk_text=args.text,
            file_name="probe.txt",
        )
    except IngestionError as exc:
        print(
            json.dumps(
                {"ok": False, "error": {"code": exc.code, "message": exc.message}},
                ensure_ascii=False,
                indent=2,
            )
        )
        return 1
    print(
        json.dumps(
            {
                "ok": True,
                "endpoint": extractor.endpoint,
                "model": extractor.model,
                "entities": [item.model_dump(mode="json") for item in batch.entities],
                "relations": [item.model_dump(mode="json") for item in batch.relations],
                "warnings": batch.warnings,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["build_parser", "main"]
