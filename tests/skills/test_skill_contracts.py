"""Contract tests shared by every skill."""

from __future__ import annotations

import ast
import hashlib
import importlib
import json
from pathlib import Path

import pytest

from skills.runtime import (
    READ_ONLY_TOOLS,
    SkillResponse,
    TreeValidation,
    quote_digest,
)

pytestmark = pytest.mark.offline

SKILL_MODULES = (
    "skills.fault_diagnosis.handler",
    "skills.evidence_verification.handler",
    "skills.ontology_change_draft.handler",
)


def _load(name: str):
    return importlib.import_module(name)


@pytest.mark.parametrize("module_name", SKILL_MODULES)
def test_each_skill_declares_a_versioned_contract(module_name):
    module = _load(module_name)

    assert module.SKILL_NAME == module_name.split(".")[1]
    assert module.SKILL_VERSION
    assert issubclass(module.RESPONSE_MODEL, SkillResponse)
    assert module.REQUEST_MODEL.model_config.get("extra") == "forbid"
    assert module.RESPONSE_MODEL.model_config.get("extra") == "forbid"
    assert callable(module.run)


@pytest.mark.parametrize("module_name", SKILL_MODULES)
def test_skills_only_reference_allow_listed_tools(module_name):
    module = _load(module_name)

    assert set(module.SKILL_TOOLS) <= READ_ONLY_TOOLS
    assert len(set(module.SKILL_TOOLS)) == len(module.SKILL_TOOLS)


@pytest.mark.parametrize("module_name", SKILL_MODULES)
def test_handlers_do_not_import_domain_or_mcp_packages(module_name):
    module = _load(module_name)
    source = Path(module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])

    assert "lightrag" not in imported
    assert "mcp_server" not in imported
    assert "mcp" not in imported


def test_read_only_allowlist_is_the_six_mcp_tools():
    assert READ_ONLY_TOOLS == {
        "get_ontology",
        "get_equipment",
        "search_faults",
        "get_fault_tree",
        "trace_evidence",
        "validate_fault_tree",
    }


def test_quote_digest_uses_the_evidence_contract_form():
    quote = "入口过滤器堵塞"
    expected = "sha256:" + hashlib.sha256(quote.encode("utf-8")).hexdigest()
    assert quote_digest(quote) == expected


def test_evidence_checks_reject_a_broken_span_and_hash():
    from skills.runtime import EvidenceRef

    ref = EvidenceRef(
        evidence_id="EV-1",
        quote="abc",
        quote_hash=quote_digest("abd"),
        asset_id="FILE-1",
        file_name="manual.md",
        char_start=0,
        char_end=4,
    )

    checks = ref.checks()
    assert checks["quote_present"]
    assert not checks["hash_matches"]
    assert not checks["span_matches"]
    assert checks["asset_present"]
    assert not ref.integrity_ok()


def test_tree_validation_projects_the_mcp_payload():
    validation = TreeValidation.from_mcp(
        {
            "draft_id": "TREE-1",
            "top_event_uid": "FAULT-1",
            "valid": False,
            "error_count": 2,
            "warning_count": 1,
            "issues": [{"code": "x"}, "ignored"],
        }
    )

    assert validation.draft_id == "TREE-1"
    assert validation.valid is False
    assert validation.error_count == 2
    assert validation.warning_count == 1
    assert validation.issues == [{"code": "x"}]


def test_generated_schemas_carry_skill_metadata():
    from scripts.export_skill_schemas import build_schemas

    schemas = build_schemas()

    assert {relative.as_posix() for relative in schemas} == {
        "fault_diagnosis/input.schema.json",
        "fault_diagnosis/output.schema.json",
        "evidence_verification/input.schema.json",
        "evidence_verification/output.schema.json",
        "ontology_change_draft/input.schema.json",
        "ontology_change_draft/output.schema.json",
    }
    for relative, schema in schemas.items():
        skill_name = relative.parts[0]
        assert schema["x-skill-name"] == skill_name
        assert schema["x-skill-version"] == "0.1.0"
        assert schema["$schema"] == (
            "https://json-schema.org/draft/2020-12/schema"
        )
    input_schema = schemas[Path("fault_diagnosis") / "input.schema.json"]
    assert input_schema["additionalProperties"] is False
    assert "equipment_ref" in input_schema["required"]


def test_written_schemas_and_manifest_are_self_consistent(tmp_path):
    from scripts.export_skill_schemas import write_schemas

    written = write_schemas(tmp_path)

    manifest_path = tmp_path / "manifest.json"
    assert manifest_path in written
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["generated_by"] == "scripts/export_skill_schemas.py"
    assert len(manifest["skills"]) == 3

    for skill in manifest["skills"]:
        for kind in ("input", "output"):
            path = tmp_path / skill["name"] / f"{kind}.schema.json"
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            assert skill["schema_sha256"][f"{kind}.schema.json"] == digest
        assert skill["tools"] == list(
            _load(skill["entrypoint"].split(":")[0]).SKILL_TOOLS
        )


def test_checked_in_schemas_match_the_contracts():
    from scripts.export_skill_schemas import SKILLS_DIR, build_schemas

    for relative, schema in build_schemas().items():
        path = SKILLS_DIR / relative
        assert path.exists(), (
            f"missing {path}; run `python scripts/export_skill_schemas.py`"
        )
        on_disk = json.loads(path.read_text(encoding="utf-8"))
        assert on_disk == schema, f"{path} is stale"


def test_checked_in_manifest_matches_the_schema_files():
    from scripts.export_skill_schemas import SKILLS_DIR

    manifest_path = SKILLS_DIR / "manifest.json"
    assert manifest_path.exists(), (
        "missing skills/manifest.json; run "
        "`python scripts/export_skill_schemas.py`"
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["runtime_version"] == "0.1.0"
    for skill in manifest["skills"]:
        for kind in ("input", "output"):
            path = SKILLS_DIR / skill["name"] / f"{kind}.schema.json"
            assert path.exists()
            assert (
                skill["schema_sha256"][f"{kind}.schema.json"]
                == hashlib.sha256(path.read_bytes()).hexdigest()
            )
        assert (SKILLS_DIR / skill["doc"]).exists()
        assert (SKILLS_DIR / skill["inputs"]).exists()
        assert (SKILLS_DIR / skill["outputs"]).exists()


@pytest.mark.parametrize(
    ("skill_name", "module_name"),
    [
        ("fault_diagnosis", "skills.fault_diagnosis.handler"),
        ("evidence_verification", "skills.evidence_verification.handler"),
        ("ontology_change_draft", "skills.ontology_change_draft.handler"),
    ],
)
def test_example_request_matches_the_input_contract(skill_name, module_name):
    from scripts.export_skill_schemas import SKILLS_DIR

    module = _load(module_name)
    path = SKILLS_DIR / skill_name / "examples" / "request.json"
    payload = json.loads(path.read_text(encoding="utf-8"))

    request = module.REQUEST_MODEL.model_validate(payload)
    assert request.tenant_id == "tenant-demo"
