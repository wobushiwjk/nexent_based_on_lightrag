"""The extraction hook and the projection service share one batch contract."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path

import pytest

from lightrag.fault_tree.contracts import FileAsset
from lightrag.fault_tree.ontology import OntologyRegistry
from lightrag.fault_tree.projection import (
    CandidateBatchInput,
    SourceChunk,
    project_domain,
)
from lightrag.fault_tree.schemas import (
    EvidenceCandidate,
    FaultEntityPayload,
)
from lightrag.fault_tree.validator import FaultCandidateValidator
from lightrag.operate import (
    _build_fault_entity_payload,
    _build_fault_relation_payload,
)

ROOT = Path(__file__).resolve().parents[2]
PACK = ROOT / "domain_packs" / "industrial_fault"
ONTOLOGY = OntologyRegistry.from_file(PACK / "ontology.yml")
TENANT = "tenant-pipeline"
TEXT = "泵 P-101A 出口压力不足，检查过滤器。"
NOW = datetime(2026, 11, 7, tzinfo=UTC)


def _file_asset() -> FileAsset:
    return FileAsset(
        uid="FILE-PIPELINE",
        tenant_id=TENANT,
        canonical_name="pipeline.md",
        ontology_version=ONTOLOGY.version,
        schema_version=ONTOLOGY.schema_version or "industrial-fault-schema-v0.1",
        review_status="approved",
        created_at=NOW,
        updated_at=NOW,
        created_by="test",
        file_name="pipeline.md",
        file_extension="md",
        mime_type="text/markdown",
        modality="text",
        object_uri="pipeline.md",
        checksum="sha256:" + hashlib.sha256(TEXT.encode()).hexdigest(),
        file_size=len(TEXT.encode()),
        uploader_id="test",
        upload_time=NOW,
        parse_status="parsed",
    )


def test_parser_preserves_projection_fields() -> None:
    entity = _build_fault_entity_payload(
        {
            "class": "FaultEvent",
            "local_id": "fault:lp",
            "uid": "FAULT-LP",
            "active_cause_rule_uid": "RULE-LP",
            "review_status": "approved",
            "confidence": 0.9,
            "cause_rule": {"op": "OR", "children": []},
        }
    )
    assert entity["uid"] == "FAULT-LP"
    assert entity["active_cause_rule_uid"] == "RULE-LP"
    assert entity["review_status"] == "approved"
    assert entity["confidence"] == 0.9

    relation = _build_fault_relation_payload(
        {
            "relation_code": "CAUSES",
            "fact_id": "FACT-1",
            "properties": {"scenario": "low load"},
            "review_status": "approved",
        }
    )
    assert relation["fact_id"] == "FACT-1"
    assert relation["properties"] == {"scenario": "low load"}
    assert relation["review_status"] == "approved"


@pytest.mark.offline
@pytest.mark.asyncio
async def test_validator_batch_projects_to_the_domain() -> None:
    payload = FaultEntityPayload(
        local_id="equipment:p101a",
        class_name="Equipment",
        evidence=[
            EvidenceCandidate(
                quote="P-101A",
                evidence_id="EV-PIPELINE",
                char_start=2,
                char_end=8,
            )
        ],
        properties={
            "uid": "EQ-P101A",
            "name": "P-101A",
            "equipment_tag": "P-101A",
            "equipment_type": "离心泵",
        },
    )
    record = {
        "entity_name": "P-101A",
        "entity_type": "Equipment",
        "source_id": "chunk-1",
        "_fault_payload": payload.model_dump_json(by_alias=True),
    }
    captured = []

    def sink(batch) -> None:
        captured.append(batch)

    validator = FaultCandidateValidator(ONTOLOGY, sink=sink)
    nodes, edges = await validator(
        "chunk-1",
        TEXT,
        {"P-101A": [record]},
        {},
    )

    assert nodes["P-101A"]
    assert edges == {}
    assert len(captured) == 1
    batch = captured[0]
    assert batch.chunk_text == TEXT

    projected = project_domain(
        ONTOLOGY,
        tenant_id=TENANT,
        batches=[
            CandidateBatchInput(
                chunk=SourceChunk(
                    asset_id="FILE-PIPELINE",
                    chunk_id="chunk-1",
                    text=TEXT,
                ),
                batch=batch,
            )
        ],
        assets=[_file_asset()],
        schema_version=ONTOLOGY.schema_version or "",
        review_status="approved",
        created_by="test",
    )

    equipment = next(
        entity for entity in projected.entities if entity.uid == "EQ-P101A"
    )
    assert equipment.equipment_type == "离心泵"
    assert equipment.evidence_ids == ["EV-PIPELINE"]
    evidence = projected.evidence[0]
    assert evidence.quote == "P-101A"
    assert evidence.targets[0].target_id == "EQ-P101A"
