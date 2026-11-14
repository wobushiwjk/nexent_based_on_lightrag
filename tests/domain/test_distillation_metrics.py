"""Tests for the W7 evaluation metrics and split-leakage check."""

from __future__ import annotations

from lightrag.fault_tree.distillation.metrics import (
    aggregate_scores,
    asset_id_from_chunk_id,
    check_split_leakage,
    entity_keys,
    group_scores,
    normalize_name,
    relation_keys,
    resolve_asset_id,
    score_sample,
    source_group,
)


def _entity(
    local_id: str,
    name: str,
    quote: str | None = None,
    cause_rule: dict | None = None,
) -> dict:
    entity = {
        "local_id": local_id,
        "class": "FaultEvent",
        "name": name,
        "evidence": [{"quote": quote if quote is not None else name}],
    }
    if cause_rule is not None:
        entity["cause_rule"] = cause_rule
    return entity


def _target(entities: list[dict], relations: list[dict] | None = None) -> dict:
    return {"entities": entities, "relations": relations or []}


def _relation(source: str, target: str, quote: str) -> dict:
    return {
        "relation_code": "CAUSES",
        "source_local_id": source,
        "target_local_id": target,
        "evidence": [{"quote": quote}],
    }


def test_normalize_name_removes_width_and_spacing_differences() -> None:
    assert normalize_name("气源压力低于 0.4 Mpa") == normalize_name(
        "气源压力低于0.4Mpa"
    )
    assert normalize_name("Ａ，Ｂ") == "A,B"


def test_exact_and_normalized_entity_keys_differ_only_on_cosmetics() -> None:
    gold = _target([_entity("g1", "油温 高", "油温高")])
    prediction = _target([_entity("p1", "油温高", "油温高")])

    assert entity_keys(prediction) == {("FaultEvent", "油温高")}
    assert entity_keys(gold) == {("FaultEvent", "油温 高")}
    assert entity_keys(prediction, normalized=True) == entity_keys(
        gold, normalized=True
    )

    score = score_sample(
        sample_id="CFDK-job_data_10-0001",
        chunk_text="油温高",
        gold=gold,
        prediction=prediction,
        json_valid=True,
        contract_valid=True,
    )
    assert score.entity_tp == 1
    assert score.entity_tp_exact == 0
    assert score.entity_pred == 1
    assert score.entity_gold == 1


def test_relation_keys_resolve_local_ids_to_names() -> None:
    target = _target(
        [_entity("a", "泵体振动"), _entity("b", "轴承磨损")],
        [_relation("a", "b", "泵体振动导致轴承磨损")],
    )
    assert relation_keys(target) == {
        ("CAUSES", "泵体振动", "轴承磨损")
    }


def test_reversed_relation_is_counted_separately() -> None:
    gold = _target(
        [_entity("a", "泵体振动"), _entity("b", "轴承磨损")],
        [_relation("a", "b", "泵体振动导致轴承磨损")],
    )
    prediction = _target(
        [_entity("x", "泵体振动"), _entity("y", "轴承磨损")],
        [_relation("y", "x", "泵体振动导致轴承磨损")],
    )
    score = score_sample(
        sample_id="CFDK-job_data_11-0001",
        chunk_text="泵体振动导致轴承磨损",
        gold=gold,
        prediction=prediction,
        json_valid=True,
        contract_valid=True,
    )
    assert score.relation_tp == 0
    assert score.reversed_relations == 1


def test_quotes_and_contract_are_scored_independently() -> None:
    gold = _target([_entity("g1", "泵体振动", "泵体振动")])
    prediction = _target([_entity("p1", "泵体振动", "原文没有这句")])
    score = score_sample(
        sample_id="CFDK-job_data_12-0001",
        chunk_text="泵体振动导致轴承磨损",
        gold=gold,
        prediction=prediction,
        json_valid=True,
        contract_valid=False,
        contract_error="quote is not verbatim",
    )
    assert score.entity_tp == 1
    assert score.quotes == 1
    assert score.quotes_verbatim == 0
    assert not score.contract_valid


def test_empty_prediction_scores_as_false_negative() -> None:
    gold = _target([_entity("g1", "泵体振动", "泵体振动")])
    score = score_sample(
        sample_id="CFDK-job_data_13-0001",
        chunk_text="泵体振动导致轴承磨损",
        gold=gold,
        prediction=None,
        json_valid=False,
        generation_error="timeout",
    )
    assert score.entity_tp == 0
    assert score.entity_gold == 1
    assert score.empty_pred
    assert score.generation_error == "timeout"


def test_cause_rule_matching_ignores_child_order() -> None:
    gold = _target(
        [
            _entity("a", "油温高", "油温高"),
            _entity("b", "冷却水中断", "冷却水中断"),
            _entity(
                "c",
                "压缩机停机",
                "压缩机停机",
                cause_rule={
                    "op": "OR",
                    "children": [
                        {"event_local_id": "a"},
                        {"event_local_id": "b"},
                    ],
                },
            ),
        ]
    )
    prediction = _target(
        [
            _entity("x", "油温高", "油温高"),
            _entity("y", "冷却水中断", "冷却水中断"),
            _entity(
                "z",
                "压缩机停机",
                "压缩机停机",
                cause_rule={
                    "op": "OR",
                    "children": [
                        {"event_local_id": "y"},
                        {"event_local_id": "x"},
                    ],
                },
            ),
        ]
    )
    score = score_sample(
        sample_id="CFDK-job_data_16-0001",
        chunk_text="油温高或冷却水中断导致压缩机停机",
        gold=gold,
        prediction=prediction,
        json_valid=True,
        contract_valid=True,
    )
    assert score.cause_rule_total == 1
    assert score.cause_rule_correct == 1
    aggregate = aggregate_scores([score])
    assert aggregate["cause_rule_accuracy"] == 1.0


def test_cause_rule_mismatch_is_not_counted_as_correct() -> None:
    gold = _target(
        [
            _entity("a", "油温高", "油温高"),
            _entity("b", "冷却水中断", "冷却水中断"),
            _entity(
                "c",
                "压缩机停机",
                "压缩机停机",
                cause_rule={"event_local_id": "a"},
            ),
        ]
    )
    prediction = _target(
        [
            _entity("x", "油温高", "油温高"),
            _entity("y", "冷却水中断", "冷却水中断"),
            _entity(
                "z",
                "压缩机停机",
                "压缩机停机",
                cause_rule={"event_local_id": "y"},
            ),
        ]
    )
    score = score_sample(
        sample_id="CFDK-job_data_17-0001",
        chunk_text="油温高导致压缩机停机",
        gold=gold,
        prediction=prediction,
        json_valid=True,
        contract_valid=True,
    )
    assert score.cause_rule_total == 1
    assert score.cause_rule_correct == 0


def test_malformed_relation_endpoint_is_counted() -> None:
    prediction = _target(
        [_entity("a", "泵体振动")],
        [_relation("a", "missing", "泵体振动")],
    )
    score = score_sample(
        sample_id="CFDK-job_data_14-0001",
        chunk_text="泵体振动",
        gold=_target([]),
        prediction=prediction,
        json_valid=True,
        contract_valid=False,
    )
    assert score.malformed_relations == 1


def test_aggregate_and_group_rates() -> None:
    gold = _target([_entity("g1", "泵体振动", "泵体振动")])
    good = score_sample(
        sample_id="CFDK-job_data_15-0001",
        chunk_text="泵体振动",
        gold=gold,
        prediction=gold,
        json_valid=True,
        contract_valid=True,
        output_tokens=10,
    )
    negative = score_sample(
        sample_id="grundfos-6859100-0001",
        chunk_text="检查并更换轴承。",
        gold=_target([]),
        prediction=_target([]),
        json_valid=True,
        contract_valid=True,
        output_tokens=2,
    )
    aggregate = aggregate_scores([good, negative])
    assert aggregate["samples"] == 2
    assert aggregate["entity"]["f1"] == 1.0
    assert aggregate["negative_accuracy"] == 1.0
    assert aggregate["json_valid_rate"] == 1.0
    assert aggregate["avg_output_tokens"] == 6.0

    groups = group_scores([good, negative])
    assert groups["cfdk"]["samples"] == 1
    assert groups["manual"]["samples"] == 1


def test_asset_id_and_group_extraction() -> None:
    assert asset_id_from_chunk_id("grundfos-6859100-0003") == "grundfos-6859100"
    assert asset_id_from_chunk_id("CFDK-job_data_10-0001") == "CFDK-job_data_10"
    assert source_group("CFDK-job_data_10") == "cfdk"
    assert source_group("grundfos-6859100") == "manual"
    assert source_group("other-doc") == "other"


def test_resolve_asset_id_handles_manual_section_chunk_ids() -> None:
    candidates = [
        "grundfos-6859100",
        "grundfos-nk-6859942",
        "CFDK-job_data_10",
    ]
    # Manual chunks are <asset>-<section>-<chunk>, so the last-hyphen fallback
    # would return "grundfos-6859100-f09" and fail the leakage check.
    assert (
        resolve_asset_id("grundfos-6859100-f09-c02", candidates)
        == "grundfos-6859100"
    )
    assert (
        resolve_asset_id("grundfos-nk-6859942-0003", candidates)
        == "grundfos-nk-6859942"
    )
    assert resolve_asset_id("unknown-doc-0001", candidates) is None


def test_leakage_check_resolves_manual_section_chunk_ids() -> None:
    manifest = {
        "assets": {
            "train": [],
            "val": [],
            "test": ["grundfos-6859100"],
        }
    }
    assert check_split_leakage(manifest, ["grundfos-6859100-f09-c02"]) == []


def test_score_sample_uses_resolved_asset_for_grouping() -> None:
    score = score_sample(
        sample_id="grundfos-6859100-f09-c02",
        chunk_text="检查并更换轴承。",
        gold=_target([]),
        prediction=_target([]),
        json_valid=True,
        contract_valid=True,
        asset_id="grundfos-6859100",
    )
    assert score.asset_id == "grundfos-6859100"
    assert score.group == "manual"


def test_leakage_check_detects_train_overlap_and_unknown_test_asset() -> None:
    manifest = {
        "assets": {
            "train": ["CFDK-job_data_1"],
            "val": ["CFDK-job_data_2"],
            "test": ["grundfos-6859100"],
        }
    }
    assert check_split_leakage(manifest, ["grundfos-6859100-0001"]) == []

    problems = check_split_leakage(
        manifest,
        ["CFDK-job_data_1-0001", "CFDK-job_data_2-0001"],
    )
    assert any("is in train" in problem for problem in problems)
    assert any("not in the manifest test split" in problem for problem in problems)

    unknown = check_split_leakage(manifest, ["CFDK-job_data_3-0001"])
    assert any("cannot match any asset" in problem for problem in unknown)


def test_leakage_check_reports_missing_assets_object() -> None:
    problems = check_split_leakage({}, ["CFDK-job_data_1-0001"])
    assert len(problems) == 1
    assert "assets" in problems[0]


def test_aggregate_of_empty_scores_is_stable() -> None:
    assert aggregate_scores([]) == {"samples": 0}
