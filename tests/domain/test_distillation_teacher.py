"""Tests for the W7 teacher client, normalizer, and public-source gate."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from lightrag.fault_tree.distillation.teacher import (
    TeacherConfig,
    TeacherError,
    TeacherOutputError,
    build_payload,
    extract_json_object,
    load_env_file,
    normalize_teacher_target,
    parse_chat_completion,
)
from scripts.label_with_teacher import main, source_is_public

ROOT = Path(__file__).resolve().parents[2]
PROMPT_PROFILE = ROOT / "prompts" / "entity_type" / "industrial_fault.yml"


def _write_jsonl(path: Path, rows: list[dict]) -> Path:
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )
    return path


def test_extract_json_object_handles_fence_and_prose() -> None:
    text = '结果如下：\n```json\n{"entities": [], "relations": []}\n```\n完成。'

    assert extract_json_object(text) == {"entities": [], "relations": []}


def test_extract_json_object_ignores_braces_inside_strings() -> None:
    text = '{"entities": [{"name": "a } b"}], "relations": []}'

    parsed = extract_json_object(text)

    assert parsed["entities"][0]["name"] == "a } b"


def test_extract_json_object_rejects_non_object() -> None:
    with pytest.raises(TeacherOutputError, match="not an object"):
        extract_json_object("[1, 2, 3]")


def test_normalize_drops_ungrounded_entity_and_prunes_rule() -> None:
    text = "过滤器堵塞和密封泄漏导致压力低。"
    raw = {
        "entities": [
            {
                "local_id": "parent",
                "class": "FaultEvent",
                "name": "压力低",
                "cause_rule": {
                    "op": "OR",
                    "children": [
                        {"event_local_id": "child"},
                        {"event_local_id": "missing"},
                    ],
                },
                "evidence": [{"quote": "压力低"}],
            },
            {
                "local_id": "child",
                "class": "FaultEvent",
                "name": "过滤器堵塞",
                "evidence": [{"quote": "过滤器堵塞"}],
            },
            {
                "local_id": "missing",
                "class": "FaultEvent",
                "name": "叶轮汽蚀",
                "evidence": [{"quote": "叶轮汽蚀"}],
            },
        ],
        "relations": [
            {
                "relation_code": "CAUSES",
                "source_local_id": "child",
                "target_local_id": "parent",
                "evidence": [{"quote": "过滤器堵塞和密封泄漏导致压力低"}],
            },
            {
                "relation_code": "CAUSES",
                "source_local_id": "missing",
                "target_local_id": "parent",
                "evidence": [{"quote": "压力低"}],
            },
        ],
    }

    target, warnings = normalize_teacher_target(raw, text)

    assert {entity["local_id"] for entity in target["entities"]} == {
        "parent",
        "child",
    }
    parent = next(
        entity
        for entity in target["entities"]
        if entity["local_id"] == "parent"
    )
    assert "cause_rule" not in parent
    assert len(target["relations"]) == 1
    assert "parent:cause-rule-pruned" in warnings
    assert "dropped-entities:1" in warnings


def test_normalize_rejects_when_every_entity_is_dropped() -> None:
    raw = {
        "entities": [
            {
                "local_id": "e",
                "class": "FaultEvent",
                "name": "叶轮汽蚀",
                "evidence": [{"quote": "叶轮汽蚀"}],
            }
        ],
        "relations": [],
    }

    with pytest.raises(TeacherOutputError, match="every teacher entity"):
        normalize_teacher_target(raw, "过滤器堵塞导致压力低。")


def test_normalize_accepts_a_genuine_negative() -> None:
    target, warnings = normalize_teacher_target(
        {"entities": [], "relations": []}, "清理过滤器。"
    )

    assert target == {"entities": [], "relations": []}
    assert warnings == []


def test_normalize_accepts_type_alias_and_grounded_aliases() -> None:
    raw = {
        "entities": [
            {
                "local_id": "e",
                "type": "FaultEvent",
                "name": "过滤器堵塞",
                "aliases": ["过滤器", "不存在的别名"],
                "evidence": [{"quote": "过滤器堵塞"}],
            }
        ],
        "relations": [],
    }

    target, _ = normalize_teacher_target(raw, "过滤器堵塞导致压力低。")

    entity = target["entities"][0]
    assert entity["class"] == "FaultEvent"
    assert entity["aliases"] == ["过滤器"]


def test_load_env_file_parses_quotes_and_comments(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_text(
        "# comment\n"
        "W7_TEACHER_ENDPOINT=https://api.example.com/v1\n"
        'W7_TEACHER_API_KEY="secret"\n'
        "W7_TEACHER_MODEL='deepseek-flash'\n",
        encoding="utf-8",
    )

    values = load_env_file(path)

    assert values["W7_TEACHER_ENDPOINT"] == "https://api.example.com/v1"
    assert values["W7_TEACHER_API_KEY"] == "secret"
    assert values["W7_TEACHER_MODEL"] == "deepseek-flash"


def test_teacher_config_requires_endpoint_and_key() -> None:
    with pytest.raises(TeacherError, match="ENDPOINT"):
        TeacherConfig.from_env({})
    with pytest.raises(TeacherError, match="API_KEY"):
        TeacherConfig.from_env({"W7_TEACHER_ENDPOINT": "http://x"})

    config = TeacherConfig.from_env(
        {
            "W7_TEACHER_ENDPOINT": "http://x/v1",
            "W7_TEACHER_API_KEY": "k",
        }
    )
    assert config.model == "deepseek-flash"
    assert config.json_mode is True
    assert config.concurrency == 2


def test_build_payload_and_parse_completion() -> None:
    config = TeacherConfig.from_env(
        {
            "W7_TEACHER_ENDPOINT": "http://x/v1",
            "W7_TEACHER_API_KEY": "k",
        }
    )
    payload = build_payload(config, "SYS", "USER")

    assert payload["temperature"] == 0
    assert payload["response_format"] == {"type": "json_object"}
    call = parse_chat_completion(
        {
            "choices": [{"message": {"content": "{}"}}],
            "usage": {"total_tokens": 3},
        }
    )
    assert call.content == "{}"
    assert call.usage == {"total_tokens": 3}
    with pytest.raises(TeacherError, match="empty content"):
        parse_chat_completion({"choices": [{"message": {"content": ""}}]})


def test_source_gate_requires_a_yaml_boolean_true() -> None:
    assert source_is_public({"public": True}) is True
    assert source_is_public({"public": "true"}) is False
    assert source_is_public({"public": False}) is False
    assert source_is_public(None) is False


def test_dry_run_counts_public_and_blocked_chunks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("W7_TEACHER_MAX_CHUNKS", raising=False)
    chunks = _write_jsonl(
        tmp_path / "chunks.jsonl",
        [
            {
                "chunk_id": "c1",
                "asset_id": "A",
                "text": "过滤器堵塞导致压力低。",
                "source_id": "public-src",
            },
            {
                "chunk_id": "c2",
                "asset_id": "B",
                "text": "清理过滤器。",
                "source_id": "private-src",
            },
        ],
    )
    sources = tmp_path / "sources.yml"
    sources.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "sources": [
                    {
                        "id": "public-src",
                        "title": "public",
                        "public": True,
                        "license": "public-dataset",
                    },
                    {
                        "id": "private-src",
                        "title": "private",
                        "public": False,
                        "license": "unknown",
                    },
                ],
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )

    exit_code = main(
        [
            "--chunks",
            str(chunks),
            "--sources",
            str(sources),
            "--prompt-profile",
            str(PROMPT_PROFILE),
            "--env-file",
            str(tmp_path / "missing.env"),
            "--out-dir",
            str(tmp_path / "out"),
            "--dry-run",
        ]
    )

    assert exit_code == 0
    plan = json.loads(
        (tmp_path / "out" / "teacher_plan.json").read_text(encoding="utf-8")
    )
    assert plan["chunks_total"] == 2
    assert plan["to_label"] == 1
    assert plan["blocked"] == 1
    assert plan["public_chunk_ids"] == ["c1"]
    assert plan["blocked_chunk_ids"] == ["c2"]


def _public_sources(tmp_path: Path) -> Path:
    sources = tmp_path / "sources.yml"
    sources.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "sources": [
                    {
                        "id": "public-src",
                        "title": "public",
                        "public": True,
                        "license": "public-dataset",
                    }
                ],
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    return sources


def test_repeatable_chunks_and_skip_label_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("W7_TEACHER_MAX_CHUNKS", raising=False)
    first = _write_jsonl(
        tmp_path / "first.jsonl",
        [
            {
                "chunk_id": "c1",
                "asset_id": "A",
                "text": "过滤器堵塞导致压力低。",
                "source_id": "public-src",
            }
        ],
    )
    second = _write_jsonl(
        tmp_path / "second.jsonl",
        [
            {
                "chunk_id": "c2",
                "asset_id": "B",
                "text": "密封泄漏导致流量下降。",
                "source_id": "public-src",
            }
        ],
    )
    bootstrap = _write_jsonl(
        tmp_path / "bootstrap.jsonl",
        [
            {
                "chunk_id": "c1",
                "asset_id": "A",
                "source_id": "public-src",
                "target": {"entities": [], "relations": []},
            }
        ],
    )
    sources = _public_sources(tmp_path)

    exit_code = main(
        [
            "--chunks",
            str(first),
            "--chunks",
            str(second),
            "--skip-label-file",
            str(bootstrap),
            "--sources",
            str(sources),
            "--prompt-profile",
            str(PROMPT_PROFILE),
            "--env-file",
            str(tmp_path / "missing.env"),
            "--out-dir",
            str(tmp_path / "out"),
            "--dry-run",
        ]
    )

    assert exit_code == 0
    plan = json.loads(
        (tmp_path / "out" / "teacher_plan.json").read_text(encoding="utf-8")
    )
    assert plan["chunks_total"] == 2
    assert plan["selected"] == 1
    assert plan["skipped_by_label_file"] == 1
    assert plan["public_chunk_ids"] == ["c2"]


def test_duplicate_chunk_id_across_manifests_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("W7_TEACHER_MAX_CHUNKS", raising=False)
    row = {
        "chunk_id": "dup",
        "asset_id": "A",
        "text": "过滤器堵塞导致压力低。",
        "source_id": "public-src",
    }
    first = _write_jsonl(tmp_path / "first.jsonl", [row])
    second = _write_jsonl(tmp_path / "second.jsonl", [row])
    sources = _public_sources(tmp_path)

    exit_code = main(
        [
            "--chunks",
            str(first),
            "--chunks",
            str(second),
            "--sources",
            str(sources),
            "--prompt-profile",
            str(PROMPT_PROFILE),
            "--env-file",
            str(tmp_path / "missing.env"),
            "--out-dir",
            str(tmp_path / "out"),
            "--dry-run",
        ]
    )

    assert exit_code == 2
    assert not (tmp_path / "out" / "teacher_plan.json").exists()
