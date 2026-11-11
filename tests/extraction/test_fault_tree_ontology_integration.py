import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from lightrag.fault_tree import (
    FaultCandidateValidator,
    FaultEntityPayload,
    OntologyRegistry,
    project_candidate_batch,
)
from lightrag.operate import _process_json_extraction_result, extract_entities
from lightrag.prompt import resolve_entity_extraction_prompt_profile
from lightrag.utils import Tokenizer, TokenizerInterface


class _DummyTokenizer(TokenizerInterface):
    def encode(self, content: str):
        return [ord(ch) for ch in content]

    def decode(self, tokens):
        return "".join(chr(token) for token in tokens)


def _profile_path() -> Path:
    return (
        Path(__file__).resolve().parents[2]
        / "prompts"
        / "entity_type"
        / "industrial_fault.yml"
    )


def _domain_result() -> str:
    return json.dumps(
        {
            "entities": [
                {
                    "local_id": "eq1",
                    "name": "冷却水泵",
                    "type": "Equipment",
                    "description": "冷却水泵 P-101A",
                    "equipment_tag": "P-101A",
                    "evidence": [{"quote": "冷却水泵 P-101A"}],
                },
                {
                    "local_id": "e1",
                    "name": "过滤器堵塞",
                    "type": "FaultEvent",
                    "description": "过滤器流通能力下降",
                    "maintenance_measures": ["清理过滤器"],
                    "evidence": [{"quote": "过滤器堵塞会造成出口压力不足"}],
                },
                {
                    "local_id": "e2",
                    "name": "出口压力不足",
                    "type": "FaultEvent",
                    "description": "泵出口压力低于正常范围",
                    "cause_rule": {"event_local_id": "e1"},
                    "evidence": [{"quote": "过滤器堵塞会造成出口压力不足"}],
                },
            ],
            "relationships": [
                {
                    "source": "冷却水泵",
                    "target": "出口压力不足",
                    "relation_code": "HAS_FAULT",
                    "keywords": "设备故障",
                    "description": "冷却水泵发生出口压力不足",
                    "evidence": [{"quote": "冷却水泵发生出口压力不足"}],
                }
            ],
        },
        ensure_ascii=False,
    )


@pytest.mark.offline
def test_industrial_profile_renders_classes_relations_and_cause_schema(monkeypatch):
    monkeypatch.setenv("PROMPT_DIR", str(_profile_path().parents[1]))

    profile = resolve_entity_extraction_prompt_profile(
        {"entity_type_prompt_file": _profile_path().name},
        use_json=True,
    )

    guidance = profile["entity_types_guidance"]
    assert "Equipment" in guidance
    assert "FaultEvent" in guidance
    assert "equipment_tag" in guidance
    assert "maintenance_measures" in guidance
    assert "candidate" not in guidance.casefold() or "Other" in guidance

    rules = profile["domain_extraction_rules"]
    assert "CAUSES: FaultEvent -> FaultEvent" in rules
    assert "HAS_FAULT" in rules
    assert "Equipment -> FaultEvent" in rules
    assert "FaultEvent.cause_rule" in rules
    assert "gate_key" in rules
    assert '"op": "AND"' in rules
    assert "evidence.quote" in rules
    assert "尽可能完整的故障传播链" in rules
    assert "不能为了" in rules
    assert "cause_rule 只能引用当前 Chunk" in rules
    assert "每个实体和关系都必须包含 evidence" in rules

    examples = profile["entity_extraction_json_examples"]
    assert len(examples) == 3

    parsed_examples = [json.loads(example) for example in examples]
    assert all(example["entities"] for example in parsed_examples)
    assert all(example["relationships"] for example in parsed_examples)
    assert all(
        entity.get("evidence")
        for example in parsed_examples
        for entity in example["entities"]
    )
    assert all(
        relationship.get("evidence")
        for example in parsed_examples
        for relationship in example["relationships"]
    )
    assert {
        rule["op"]
        for example in parsed_examples
        for entity in example["entities"]
        for rule in [entity.get("cause_rule")]
        if isinstance(rule, dict) and "op" in rule
    } == {"AND", "OR"}
    assert all(
        relationship["relation_code"] in {"CAUSES", "HAS_FAULT"}
        for example in parsed_examples
        for relationship in example["relationships"]
    )


@pytest.mark.offline
@pytest.mark.asyncio
async def test_domain_rules_are_injected_into_existing_json_prompt(
    monkeypatch,
):
    monkeypatch.setenv("PROMPT_DIR", str(_profile_path().parents[1]))
    chunks = {
        "chunk-1": {
            "tokens": 10,
            "content": "冷却水泵 P-101A 发生出口压力不足。",
            "full_doc_id": "doc-1",
            "chunk_order_index": 0,
            "file_path": "manual.md",
        }
    }
    extracted = json.dumps({"entities": [], "relationships": []})
    llm = AsyncMock(return_value=extracted)
    tokenizer = Tokenizer("dummy", _DummyTokenizer())
    profile = resolve_entity_extraction_prompt_profile(
        {"entity_type_prompt_file": _profile_path().name},
        use_json=True,
    )

    global_config = {
        "llm_model_func": llm,
        "role_llm_funcs": {
            "extract": llm,
            "keyword": llm,
            "query": llm,
            "vlm": llm,
        },
        "entity_extract_max_gleaning": 0,
        "entity_extract_max_records": 100,
        "entity_extract_max_entities": 40,
        "entity_extraction_use_json": True,
        "addon_params": {"language": "Chinese"},
        "tokenizer": tokenizer,
        "llm_model_max_async": 2,
        "kg_extraction_validator": None,
        "_entity_extraction_prompt_profile": profile,
    }

    await extract_entities(chunks=chunks, global_config=global_config)

    system_prompt = llm.await_args.kwargs["system_prompt"]
    assert "---Domain Extraction Rules---" in system_prompt
    assert "CAUSES: FaultEvent -> FaultEvent" in system_prompt
    assert "HAS_FAULT: Equipment -> FaultEvent" in system_prompt
    assert "FaultEvent.cause_rule" in system_prompt
    assert "冷却水回路堵塞" in system_prompt
    assert "压力变送器零点漂移" in system_prompt


@pytest.mark.offline
@pytest.mark.asyncio
async def test_json_parser_preserves_fault_domain_payload():
    raw = _domain_result()
    nodes, edges = await _process_json_extraction_result(
        raw,
        chunk_key="chunk-1",
        timestamp=1,
    )

    entity_payload = json.loads(nodes["出口压力不足"][0]["_fault_payload"])
    assert entity_payload["local_id"] == "e2"
    assert entity_payload["class"] == "FaultEvent"
    assert entity_payload["cause_rule"] == {"event_local_id": "e1"}

    (edge_records,) = edges.values()
    relation_payload = json.loads(edge_records[0]["_fault_payload"])
    assert relation_payload["relation_code"] == "HAS_FAULT"


@pytest.mark.offline
@pytest.mark.asyncio
async def test_validator_checks_ontology_and_emits_candidate_batch():
    ontology = OntologyRegistry.from_file(_profile_path())
    batches = []
    validator = FaultCandidateValidator(ontology, sink=batches.append)

    raw = _domain_result()
    nodes, edges = await _process_json_extraction_result(
        raw,
        chunk_key="chunk-1",
        timestamp=1,
    )
    chunk_text = (
        "冷却水泵 P-101A。过滤器堵塞会造成出口压力不足。"
        "冷却水泵发生出口压力不足。"
    )

    nodes, edges = await validator(
        "chunk-1",
        chunk_text,
        nodes,
        edges,
    )

    assert nodes["出口压力不足"][0]["entity_type"] == "FaultEvent"
    assert len(edges) == 1
    assert edges[("冷却水泵", "出口压力不足")][0]["keywords"] == (
        "HAS_FAULT, 设备故障"
    )
    assert len(batches) == 1
    assert {entity.local_id for entity in batches[0].entities} == {
        "eq1",
        "e1",
        "e2",
    }
    assert batches[0].relations[0].relation_code == "HAS_FAULT"
    assert batches[0].warnings == []


@pytest.mark.offline
@pytest.mark.asyncio
async def test_validator_drops_relation_without_verbatim_evidence():
    ontology = OntologyRegistry.from_file(_profile_path())
    batches = []
    validator = FaultCandidateValidator(ontology, sink=batches.append)

    raw = _domain_result()
    nodes, edges = await _process_json_extraction_result(
        raw,
        chunk_key="chunk-1",
        timestamp=1,
    )
    chunk_text = "冷却水泵 P-101A。过滤器堵塞会造成出口压力不足。"

    nodes, edges = await validator(
        "chunk-1",
        chunk_text,
        nodes,
        edges,
    )

    assert len(nodes) == 3
    assert edges == {}
    assert batches[0].relations == []
    assert any("no verbatim evidence" in warning for warning in batches[0].warnings)


@pytest.mark.offline
def test_fault_entity_payload_rejects_single_child_and_or():
    with pytest.raises(ValueError, match="at least two children"):
        FaultEntityPayload.model_validate(
            {
                "local_id": "e1",
                "class": "FaultEvent",
                "cause_rule": {
                    "op": "AND",
                    "children": [{"event_local_id": "e2"}],
                },
                "evidence": [{"quote": "x"}],
            }
        )


@pytest.mark.offline
@pytest.mark.asyncio
async def test_cross_chunk_or_causes_share_one_stable_gate():
    ontology = OntologyRegistry.from_file(_profile_path())
    validator = FaultCandidateValidator(ontology)

    def chunk_result(
        chunk_number: int,
        cause_local_id: str,
        cause_name: str,
    ) -> str:
        return json.dumps(
            {
                "entities": [
                    {
                        "local_id": cause_local_id,
                        "name": cause_name,
                        "type": "FaultEvent",
                        "description": cause_name,
                        "evidence": [{"quote": cause_name}],
                    },
                    {
                        "local_id": f"effect-{chunk_number}",
                        "name": "浮点例外",
                        "type": "FaultEvent",
                        "description": "浮点例外",
                        "evidence": [{"quote": "浮点例外"}],
                    },
                ],
                "relationships": [
                    {
                        "source": cause_name,
                        "target": "浮点例外",
                        "relation_code": "CAUSES",
                        "gate_type_hint": "OR",
                        "gate_key": "浮点例外_原因",
                        "keywords": "故障因果",
                        "description": f"{cause_name}导致浮点例外",
                        "evidence": [{"quote": cause_name}],
                    }
                ],
            },
            ensure_ascii=False,
        )

    gate_names: list[str] = []
    for number, cause in enumerate(("指令无效", "被零除"), start=1):
        raw = chunk_result(number, f"cause-{number}", cause)
        nodes, edges = await _process_json_extraction_result(
            raw,
            chunk_key=f"doc-1-chunk-{number}",
            timestamp=number,
        )
        nodes, edges = await validator(
            f"doc-1-chunk-{number}",
            f"浮点例外 原因 {cause}",
            nodes,
            edges,
        )
        gate_names.extend(name for name in nodes if name.startswith("GATE-OR-"))
        gate_name = next(name for name in nodes if name.startswith("GATE-OR-"))
        assert edges[(cause, gate_name)][0]["keywords"] == "INPUT_OF"
        assert edges[(gate_name, "浮点例外")][0]["keywords"] == "OUTPUT_TO"
        assert not any(
            key == (cause, "浮点例外") for key in edges
        )

    assert len(set(gate_names)) == 1


@pytest.mark.offline
@pytest.mark.asyncio
async def test_validated_candidates_project_to_fault_graph():
    ontology = OntologyRegistry.from_file(_profile_path())
    validator = FaultCandidateValidator(ontology)
    raw = json.dumps(
        {
            "entities": [
                {
                    "local_id": "eq1",
                    "name": "冷却水泵",
                    "type": "Equipment",
                    "description": "冷却水泵",
                    "equipment_tag": "P-101A",
                    "evidence": [{"quote": "冷却水泵 P-101A"}],
                },
                *[
                    {
                        "local_id": local_id,
                        "name": name,
                        "type": "FaultEvent",
                        "description": name,
                        "evidence": [{"quote": name}],
                    }
                    for local_id, name in [
                        ("eA", "A"),
                        ("eB", "B"),
                        ("eC", "C"),
                        ("eD", "D"),
                    ]
                ],
                {
                    "local_id": "eE",
                    "name": "E",
                    "type": "FaultEvent",
                    "description": "E",
                    "cause_rule": {
                        "op": "AND",
                        "children": [
                            {
                                "op": "OR",
                                "children": [
                                    {"event_local_id": "eA"},
                                    {"event_local_id": "eB"},
                                ],
                            },
                            {
                                "op": "OR",
                                "children": [
                                    {"event_local_id": "eC"},
                                    {"event_local_id": "eD"},
                                ],
                            },
                        ],
                    },
                    "evidence": [{"quote": "E"}],
                },
            ],
            "relationships": [],
        },
        ensure_ascii=False,
    )
    nodes, edges = await _process_json_extraction_result(
        raw,
        chunk_key="chunk-1",
        timestamp=1,
    )
    captured = []
    validator = FaultCandidateValidator(ontology, sink=captured.append)
    await validator("chunk-1", "冷却水泵 P-101A A B C D E", nodes, edges)

    graph = project_candidate_batch(captured[0])
    labels = [node["label"] for node in graph.nodes]
    assert labels.count("Equipment") == 1
    assert labels.count("FaultEvent") == 5
    assert labels.count("FaultCauseRule") == 1
    assert labels.count("LogicGate") == 3

    edges_by_type: dict[str, list[dict]] = {}
    for edge in graph.edges:
        edges_by_type.setdefault(edge["type"], []).append(edge)
    assert len(edges_by_type["INPUT_OF"]) == 6
    assert len(edges_by_type["OUTPUT_TO"]) == 3
    gate_uids = {
        node["uid"] for node in graph.nodes if node["label"] == "LogicGate"
    }
    assert (
        sum(
            1
            for edge in edges_by_type["OUTPUT_TO"]
            if edge["source_uid"] in gate_uids
            and edge["target_uid"] in gate_uids
        )
        == 2
    )
    assert any(
        node["properties"].get("gate_type") == "AND"
        for node in graph.nodes
        if node["label"] == "LogicGate"
    )
