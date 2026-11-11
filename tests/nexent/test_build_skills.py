"""Offline tests for Skill ZIP packaging.

The builder reads ``skills/`` and ``nexent/config/skills.json`` from the
repository; the outputs are written to ``tmp_path`` only.
"""

from __future__ import annotations

import json
import zipfile

from nexent.scripts import build_skills

EXPECTED = {
    "fault-diagnosis": "fault_diagnosis",
    "evidence-verification": "evidence_verification",
    "ontology-change-draft": "ontology_change_draft",
}


def build(out_dir):
    return build_skills.build_all(out_dir=out_dir)


def test_builds_one_zip_per_configured_skill(tmp_path):
    report = build(tmp_path)

    assert {entry["name"] for entry in report["skills"]} == set(EXPECTED)
    assert len(report["skills"]) == 3
    assert (tmp_path / "manifest.json").is_file()
    for name in EXPECTED:
        assert (tmp_path / f"{name}.zip").is_file()


def test_zip_contains_skill_md_schemas_and_example(tmp_path):
    build(tmp_path)

    with zipfile.ZipFile(tmp_path / "fault-diagnosis.zip") as archive:
        names = set(archive.namelist())
        skill_md = archive.read("fault-diagnosis/SKILL.md").decode("utf-8")

    assert names == {
        "fault-diagnosis/SKILL.md",
        "fault-diagnosis/input.schema.json",
        "fault-diagnosis/output.schema.json",
        "fault-diagnosis/examples/request.json",
    }
    assert "name: fault-diagnosis" in skill_md
    assert "## 在 Nexent 中执行" in skill_md
    assert "trace_evidence" in skill_md


def test_build_is_deterministic(tmp_path):
    first = build(tmp_path / "first")
    second = build(tmp_path / "second")

    first_hashes = {entry["name"]: entry["sha256"] for entry in first["skills"]}
    second_hashes = {entry["name"]: entry["sha256"] for entry in second["skills"]}
    assert first_hashes == second_hashes


def test_check_mode_matches_disk_then_detects_drift(tmp_path):
    build(tmp_path)

    assert build_skills.build_all(out_dir=tmp_path, check=True)["ok"] is True

    (tmp_path / "fault-diagnosis.zip").write_bytes(b"corrupted")

    assert build_skills.build_all(out_dir=tmp_path, check=True)["ok"] is False


def test_config_covers_every_manifest_skill():
    manifest = build_skills.load_skill_manifest(build_skills.DEFAULT_SKILLS_ROOT)
    config = build_skills.load_config(build_skills.DEFAULT_CONFIG)

    assert {spec.source for spec in config} == set(manifest)
    assert {spec.name for spec in config} == set(EXPECTED)


def test_rewritten_skill_md_carries_mcp_server_name(tmp_path):
    build(tmp_path)

    with zipfile.ZipFile(tmp_path / "evidence-verification.zip") as archive:
        skill_md = archive.read("evidence-verification/SKILL.md").decode("utf-8")

    assert "industrial-fault-domain" in skill_md


def test_manifest_version_is_stable():
    manifest = json.loads(
        (build_skills.DEFAULT_SKILLS_ROOT / "manifest.json").read_text(
            encoding="utf-8"
        )
    )

    assert manifest["manifest_version"] == "0.1.0"
    assert manifest["runtime_version"] == "0.1.0"
