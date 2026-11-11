"""
从 handler 的 Pydantic 请求/响应模型生成 JSON Schema，再生成包含入口、工具清单和 Schema 哈希的 manifest。
Pydantic 模型是契约的事实来源，生成文件不应手改。
"""

from __future__ import annotations

import hashlib
import importlib
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SKILL_MODULES = (
    "skills.fault_diagnosis.handler",
    "skills.evidence_verification.handler",
    "skills.ontology_change_draft.handler",
)
SKILLS_DIR = ROOT / "skills"
SCHEMA_BASE = "https://pump-copilot.local/skills"


def build_schemas() -> dict[Path, dict[str, Any]]:
    """Return each schema keyed by its path relative to ``skills/``."""

    schemas: dict[Path, dict[str, Any]] = {}
    for module_name in SKILL_MODULES:
        module = importlib.import_module(module_name)
        name = module.SKILL_NAME
        for kind, model in (
            ("input", module.REQUEST_MODEL),
            ("output", module.RESPONSE_MODEL),
        ):
            schema = model.model_json_schema()
            schema = {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                **schema,
            }
            schema.setdefault(
                "$id", f"{SCHEMA_BASE}/{name}/{kind}.schema.json"
            )
            schema.setdefault(
                "title", f"{name} {kind}"
            )
            schema["x-skill-name"] = name
            schema["x-skill-version"] = module.SKILL_VERSION
            schemas[Path(name) / f"{kind}.schema.json"] = schema
    return schemas


def build_manifest() -> dict[str, Any]:
    """Return the skill manifest, including schema hashes for drift checks."""

    return build_manifest_from(SKILLS_DIR)


def write_schemas(output_dir: Path = SKILLS_DIR) -> list[Path]:
    """Write every skill schema and the manifest, returning written paths."""

    output_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for relative, schema in build_schemas().items():
        path = output_dir / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps(schema, ensure_ascii=False, indent=2, sort_keys=True)
        path.write_text(text + "\n", encoding="utf-8")
        written.append(path)

    # The manifest hashes the files just written in the real skills directory.
    if output_dir == SKILLS_DIR:
        manifest = build_manifest()
    else:
        manifest = build_manifest_from(output_dir)
    manifest_path = output_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )
    written.append(manifest_path)
    return written


def build_manifest_from(output_dir: Path) -> dict[str, Any]:
    """Build a manifest whose hashes point at ``output_dir``."""

    skills: list[dict[str, Any]] = []
    for module_name in SKILL_MODULES:
        module = importlib.import_module(module_name)
        name = module.SKILL_NAME
        files: dict[str, str] = {}
        for kind in ("input", "output"):
            path = output_dir / name / f"{kind}.schema.json"
            files[f"{kind}.schema.json"] = hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
        skills.append(
            {
                "name": name,
                "version": module.SKILL_VERSION,
                "entrypoint": f"{module_name}:run",
                "tools": list(module.SKILL_TOOLS),
                "doc": f"{name}/SKILL.md",
                "inputs": f"{name}/input.schema.json",
                "outputs": f"{name}/output.schema.json",
                "schema_sha256": files,
            }
        )
    return {
        "manifest_version": "0.1.0",
        "runtime_version": "0.1.0",
        "generated_by": "scripts/export_skill_schemas.py",
        "skills": skills,
    }


if __name__ == "__main__":
    for written_path in write_schemas():
        print(written_path.relative_to(ROOT))
