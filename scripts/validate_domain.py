"""Validate the industrial fault-tree domain pack.

Usage:
    python scripts/validate_domain.py
    python scripts/validate_domain.py --pack domain_packs/industrial_fault
    python scripts/validate_domain.py --json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from lightrag.fault_tree.domain_validation import (  # noqa: E402
    errors_only,
    load_domain_bundle,
    validate_crosswalk,
    validate_domain_bundle,
    validate_schema_manifest,
)
from lightrag.fault_tree.ontology import OntologyRegistry  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--pack",
        type=Path,
        default=ROOT / "domain_packs" / "industrial_fault",
        help="domain pack directory",
    )
    parser.add_argument(
        "--bundle",
        type=Path,
        default=None,
        help="bundle JSON; defaults to <pack>/seeds/demo_bundle.json",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="print issues as JSON instead of a summary",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    pack = args.pack.resolve()
    bundle_path = (
        args.bundle.resolve()
        if args.bundle is not None
        else pack / "seeds" / "demo_bundle.json"
    )
    ontology = OntologyRegistry.from_file(pack / "ontology.yml")
    bundle = load_domain_bundle(bundle_path)

    issues = [
        *validate_domain_bundle(bundle, pack),
        *validate_crosswalk(pack),
        *validate_schema_manifest(pack),
    ]
    errors = errors_only(issues)

    if args.json:
        print(
            json.dumps(
                [issue.model_dump(mode="json") for issue in issues],
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        print(f"Domain pack: {pack}")
        print(
            f"Ontology: {ontology.version} "
            f"({len(ontology.classes)} classes, "
            f"{len(ontology.relations)} relations)"
        )
        print(
            f"Bundle: {bundle.bundle_version} "
            f"({len(bundle.entities)} entities, "
            f"{len(bundle.evidence)} evidence, "
            f"{len(bundle.facts)} facts, "
            f"{len(bundle.rules)} rules)"
        )
        warning_count = len(issues) - len(errors)
        print(f"Issues: {len(errors)} errors, {warning_count} warnings")
        for issue in issues:
            target = f" [{issue.target_id}]" if issue.target_id else ""
            print(f"  {issue.severity.upper():7} {issue.code}{target}: {issue.message}")

    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
