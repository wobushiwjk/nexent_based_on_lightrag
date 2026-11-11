"""Package the repository Skills as Nexent-importable ZIP files.

Nexent imports a Skill as a ZIP whose first ``SKILL.md`` member defines the
skill; the ZIP's top-level directory name becomes the skill name when the
importer passes no explicit name, and ``import_to_nexent`` passes the same
name explicitly. This module produces that ZIP deterministically:

* repository skill names use underscores (``fault_diagnosis``) because they are
  Python packages, while Nexent skill names use hyphens (``fault-diagnosis``)
  to match its naming style; prose cross-references are rewritten, code fences
  are not;
* the frontmatter is re-emitted with only the keys ``SkillLoader`` keeps
  (``name``, ``description``, ``allowed-tools``, ``tags``) — ``version`` is
  dropped by the loader, so keeping it would only mislead a reader;
* ``allowed-tools`` comes from ``skills/manifest.json``, which also pins the
  schema hashes — a stale manifest fails packaging instead of producing a ZIP
  whose schemas disagree with the registry;
* the ``## 离线复现`` section is replaced by one that describes how the Agent
  drives the Skill inside Nexent, because the repository's CLI is not present
  in that runtime.

The ZIP is byte-for-byte reproducible: members are sorted, timestamps fixed,
and permissions fixed, so ``--check`` can prove the committed build output (if
any) matches the source Skills.

Run from the repository root::

    .venv/Scripts/python.exe -m nexent.scripts.build_skills
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import sys
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SKILLS_ROOT = REPO_ROOT / "skills"
DEFAULT_CONFIG = REPO_ROOT / "nexent" / "config" / "skills.json"
DEFAULT_OUT = REPO_ROOT / "nexent" / "build" / "skills"

MANIFEST_NAME = "manifest.json"
FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.DOTALL)
OFFLINE_SECTION_RE = re.compile(
    r"^## 离线复现[ \t]*\n(.*?)(?=^## |\Z)", re.MULTILINE | re.DOTALL
)
# Fixed timestamp for reproducible archives (the ZIP epoch).
ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)
ZIP_FILE_MODE = 0o644 << 16


class SkillPackagingError(RuntimeError):
    """A Skill cannot be packaged for Nexent."""


@dataclass(frozen=True)
class SkillSpec:
    """One repository Skill mapped to its Nexent name."""

    source: str
    name: str
    tags: tuple[str, ...]


def hyphenate(name: str) -> str:
    """Return the Nexent form of a repository skill name."""
    return name.replace("_", "-")


def load_skill_manifest(skills_root: Path) -> dict[str, dict[str, Any]]:
    """Load ``skills/manifest.json`` keyed by skill name."""
    path = skills_root / MANIFEST_NAME
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SkillPackagingError(f"cannot read {path}: {exc}") from exc
    entries = data.get("skills")
    if not isinstance(entries, list):
        raise SkillPackagingError(f"{path} has no 'skills' list")
    return {
        entry["name"]: entry
        for entry in entries
        if isinstance(entry, dict) and isinstance(entry.get("name"), str)
    }


def load_config(path: Path) -> list[SkillSpec]:
    """Load ``nexent/config/skills.json``."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SkillPackagingError(f"cannot read {path}: {exc}") from exc
    raw_skills = data.get("skills")
    if not isinstance(raw_skills, list) or not raw_skills:
        raise SkillPackagingError(f"{path} must list at least one skill")
    specs: list[SkillSpec] = []
    seen: set[str] = set()
    for entry in raw_skills:
        source = entry.get("source")
        name = entry.get("name")
        if not isinstance(source, str) or not isinstance(name, str):
            raise SkillPackagingError(
                f"{path}: every skill needs string 'source' and 'name'"
            )
        if name in seen:
            raise SkillPackagingError(f"{path}: duplicate skill name {name!r}")
        seen.add(name)
        specs.append(
            SkillSpec(
                source=source,
                name=name,
                tags=tuple(str(tag) for tag in entry.get("tags") or ()),
            )
        )
    return specs


def parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Split a ``SKILL.md`` into its YAML frontmatter and body."""
    match = FRONTMATTER_RE.match(text)
    if match is None:
        raise SkillPackagingError("SKILL.md has no YAML frontmatter")
    try:
        meta = yaml.safe_load(match.group(1))
    except yaml.YAMLError as exc:
        raise SkillPackagingError(f"SKILL.md frontmatter is not valid YAML: {exc}")
    if not isinstance(meta, dict):
        raise SkillPackagingError("SKILL.md frontmatter must be a mapping")
    return meta, match.group(2)


def render_frontmatter(
    name: str,
    description: str,
    allowed_tools: Iterable[str],
    tags: Iterable[str],
) -> str:
    """Render the subset of frontmatter that Nexent's SkillLoader keeps."""
    payload: dict[str, Any] = {"name": name, "description": description}
    tools = sorted({tool for tool in allowed_tools if tool})
    if tools:
        payload["allowed-tools"] = tools
    tag_list = [tag for tag in tags if tag]
    if tag_list:
        payload["tags"] = tag_list
    yaml_text = yaml.safe_dump(
        payload,
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
        width=1024,
    )
    return f"---\n{yaml_text}---\n"


def _rewrite_skill_references(
    line: str, rename: Mapping[str, str]
) -> str:
    """Rewrite whole-word repository skill names on one prose line."""
    if not rename:
        return line
    pattern = re.compile(
        r"(?<![\w/-])(" + "|".join(re.escape(name) for name in rename) + r")(?![\w/-])"
    )
    return pattern.sub(lambda match: rename[match.group(1)], line)


def rewrite_prose(text: str, rename: Mapping[str, str]) -> str:
    """Rewrite skill references outside fenced code blocks only.

    A fenced block may contain CLI flags (``--skill fault_diagnosis``) or file
    paths (``skills/fault_diagnosis/...``) that must keep the repository name.
    """
    lines = text.splitlines(keepends=True)
    in_fence = False
    rewritten: list[str] = []
    for line in lines:
        stripped = line.lstrip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_fence = not in_fence
            rewritten.append(line)
            continue
        rewritten.append(line if in_fence else _rewrite_skill_references(line, rename))
    return "".join(rewritten)


def _nexent_section(spec: SkillSpec, source: str, mcp_server_name: str) -> str:
    """Build the Nexent runtime section that replaces ``## 离线复现``."""
    return (
        "## 在 Nexent 中执行\n\n"
        f"本 Skill 以 `{spec.name}` 的名字导入 Nexent（源目录 `skills/{source}`），"
        "由 Agent 在对话中按上述步骤调用 MCP 工具；Skill 本身不包含可执行代码。\n\n"
        f"- 工具来源：Nexent 中已注册的 MCP 服务 `{mcp_server_name}`（只读工具）。\n"
        "- `tenant_id` 由 Nexent 根据登录会话注入，Agent 不需要、也不允许填写；"
        "其余输入由 Agent 从用户消息中提取，字段见 [input.schema.json](input.schema.json)。\n"
        "- 输出是统一信封 `{ok, status, data, error, warnings, tool_calls, meta}`，"
        "Agent 只复述 `data` 中的内容。\n\n"
        "仓库内离线复现（不依赖 Nexent，供开发与回归使用）：\n\n"
        "```bash\n"
        ".venv/Scripts/python.exe scripts/run_skill.py \\\n"
        f"  --skill {source} \\\n"
        f"  --input skills/{source}/examples/request.json\n"
        "```\n"
    )


def render_skill_md(
    spec: SkillSpec,
    meta: Mapping[str, Any],
    body: str,
    allowed_tools: Iterable[str],
    rename: Mapping[str, str],
    mcp_server_name: str,
) -> str:
    """Render the ``SKILL.md`` that goes into the Nexent ZIP."""
    description = meta.get("description")
    if not isinstance(description, str) or not description.strip():
        raise SkillPackagingError(f"skill {spec.source!r} has no description")

    body = OFFLINE_SECTION_RE.sub(
        lambda _match: _nexent_section(spec, spec.source, mcp_server_name) + "\n\n",
        body,
        count=1,
    )
    body = rewrite_prose(body, rename)

    frontmatter = render_frontmatter(
        spec.name, description.strip(), allowed_tools, spec.tags
    )
    return f"{frontmatter}\n{body.strip()}\n"


def read_json_with_hash(
    path: Path, expected_sha256: str | None
) -> tuple[bytes, dict[str, Any]]:
    """Read a JSON asset and enforce the manifest's schema hash."""
    try:
        content = path.read_bytes()
        payload = json.loads(content.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SkillPackagingError(f"cannot read {path}: {exc}") from exc
    actual = hashlib.sha256(content).hexdigest()
    if expected_sha256 is not None and actual != expected_sha256:
        raise SkillPackagingError(
            f"{path} does not match skills/manifest.json "
            f"({actual} != {expected_sha256}); regenerate the manifest with "
            "scripts/export_skill_schemas.py"
        )
    return content, payload


def zip_files(name: str, files: Mapping[str, bytes]) -> bytes:
    """Build a deterministic ZIP rooted at ``name/``."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for relative in sorted(files):
            info = zipfile.ZipInfo(f"{name}/{relative}", date_time=ZIP_TIMESTAMP)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = ZIP_FILE_MODE
            info.create_system = 3
            archive.writestr(info, files[relative])
    return buffer.getvalue()


def build_skill_zip(
    skills_root: Path,
    spec: SkillSpec,
    manifest_entry: Mapping[str, Any],
    rename: Mapping[str, str],
    mcp_server_name: str,
) -> tuple[bytes, dict[str, Any]]:
    """Build one Skill ZIP and return it with a summary record."""
    skill_dir = skills_root / spec.source
    skill_md = skill_dir / "SKILL.md"
    try:
        text = skill_md.read_text(encoding="utf-8")
    except OSError as exc:
        raise SkillPackagingError(f"cannot read {skill_md}: {exc}") from exc
    meta, body = parse_frontmatter(text)
    if meta.get("name") != spec.source:
        raise SkillPackagingError(
            f"{skill_md} declares name {meta.get('name')!r}, expected {spec.source!r}"
        )

    manifest_tools = manifest_entry.get("tools")
    if not isinstance(manifest_tools, list) or not manifest_tools:
        raise SkillPackagingError(
            f"skills/manifest.json has no tools for {spec.source!r}"
        )

    files: dict[str, bytes] = {}
    files["SKILL.md"] = render_skill_md(
        spec, meta, body, manifest_tools, rename, mcp_server_name
    ).encode("utf-8")

    hashes = manifest_entry.get("schema_sha256") or {}
    input_rel = manifest_entry.get("inputs")
    output_rel = manifest_entry.get("outputs")
    if not isinstance(input_rel, str) or not isinstance(output_rel, str):
        raise SkillPackagingError(
            f"skills/manifest.json has no input/output schema path for {spec.source!r}"
        )
    for relative, key in ((input_rel, "input.schema.json"), (output_rel, "output.schema.json")):
        content, _ = read_json_with_hash(
            skills_root / relative, hashes.get(key)
        )
        files[key] = content

    example = skill_dir / "examples" / "request.json"
    try:
        example_bytes = example.read_bytes()
        json.loads(example_bytes.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SkillPackagingError(f"cannot read {example}: {exc}") from exc
    files["examples/request.json"] = example_bytes

    archive = zip_files(spec.name, files)
    summary = {
        "source": spec.source,
        "name": spec.name,
        "path": f"{spec.name}.zip",
        "sha256": hashlib.sha256(archive).hexdigest(),
        "size": len(archive),
        "allowed_tools": sorted(set(manifest_tools)),
        "files": sorted(files),
    }
    return archive, summary


def build_all(
    skills_root: Path = DEFAULT_SKILLS_ROOT,
    config_path: Path = DEFAULT_CONFIG,
    out_dir: Path = DEFAULT_OUT,
    *,
    mcp_server_name: str = "industrial-fault-domain",
    check: bool = False,
) -> dict[str, Any]:
    """Build every configured Skill ZIP.

    With ``check=True`` nothing is written; the function reports whether the
    files in ``out_dir`` already match a fresh build.
    """
    manifest = load_skill_manifest(skills_root)
    specs = load_config(config_path)

    unknown = sorted({spec.source for spec in specs} - set(manifest))
    if unknown:
        raise SkillPackagingError(
            f"{config_path} references skills absent from the manifest: {unknown}"
        )
    missing = sorted(set(manifest) - {spec.source for spec in specs})
    if missing:
        raise SkillPackagingError(
            f"{config_path} does not cover every manifest skill: {missing}"
        )

    rename = {spec.source: spec.name for spec in specs}
    results: list[dict[str, Any]] = []
    for spec in specs:
        archive, summary = build_skill_zip(
            skills_root, spec, manifest[spec.source], rename, mcp_server_name
        )
        results.append(summary)
        target = out_dir / summary["path"]
        if check:
            try:
                current = target.read_bytes()
            except OSError:
                current = b""
            summary["matches_disk"] = hashlib.sha256(current).hexdigest() == summary["sha256"]
        else:
            out_dir.mkdir(parents=True, exist_ok=True)
            target.write_bytes(archive)

    report = {
        "generated_by": "nexent/scripts/build_skills.py",
        "mcp_server_name": mcp_server_name,
        "skills": results,
    }
    if check:
        report["ok"] = all(item["matches_disk"] for item in results)
    else:
        (out_dir / MANIFEST_NAME).write_text(
            json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    return report


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skills-root", default=str(DEFAULT_SKILLS_ROOT))
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument(
        "--mcp-server-name",
        default="industrial-fault-domain",
        help="Nexent MCP service name named in the generated Skill section.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Do not write; fail when the build output does not match the sources.",
    )
    args = parser.parse_args(argv)
    try:
        report = build_all(
            Path(args.skills_root).expanduser().resolve(),
            Path(args.config).expanduser().resolve(),
            Path(args.out).expanduser().resolve(),
            mcp_server_name=args.mcp_server_name,
            check=args.check,
        )
    except SkillPackagingError as exc:
        print(f"[fail] {exc}", file=sys.stderr)
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report.get("ok", True) else 1


if __name__ == "__main__":
    raise SystemExit(main())
