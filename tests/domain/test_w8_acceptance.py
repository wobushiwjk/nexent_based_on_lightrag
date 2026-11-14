"""Regression test for the isolated W8 acceptance workflow."""

from __future__ import annotations

from pathlib import Path

from scripts.w8_acceptance import run_acceptance


def test_w8_acceptance_conflict_publish_schema_and_rollback(
    tmp_path: Path,
) -> None:
    report = run_acceptance(tmp_path)

    assert report["ok"] is True
    assert report["legacy_change_id"] == "CHG-LEGACY-SENSOR"
    assert report["conflict"]["conflicting_change_ids"] == ["CHG-LEGACY-SENSOR"]
    assert report["rejected_status"] == "rejected"
    assert report["change_id"] == "CHG-0003"
    assert report["operation_count"] == 3
    assert len(report["distinct_evidence_ids"]) == 3
    assert "entity.schema.json" in report["dry_run_schema_artifacts"]
    assert report["published_status"] == "published"
    assert report["published_version"] == "industrial-fault-v0.2"
    assert report["schema_version"] == "industrial-fault-schema-v0.2"
    assert report["rolled_back_status"] == "rolled_back"
    assert report["final_active_version"] == "industrial-fault-v0.1"
    assert {run["ontology_version"] for run in report["snapshots_after_rollback"]} == {
        "industrial-fault-v0.1",
        "industrial-fault-v0.2",
    }
