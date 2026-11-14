"""Scoring helpers for the W7 student evaluation.

The metrics are deliberately pure: they take one parsed prediction, the gold
target, and the source chunk text, and return counts.  ``scripts/evaluate_distillation.py``
owns model loading and generation; this module owns the definitions so a test
can pin them without a GPU or a served model.

Two name comparisons are reported.  ``exact`` compares ``(class, name)`` after
only trimming whitespace; ``normalized`` additionally applies Unicode NFKC and
removes all whitespace, so ``气源压力低于 0.4 Mpa`` and
``气源压力低于0.4Mpa`` match.  Normalized F1 is the selection metric because
spacing is not a semantic error; exact F1 stays in the report so the stricter
number is never hidden.  A ``cause_rule`` is compared as a canonical tree:
AND/OR child order is ignored, local ids are resolved to names, and a rule
attached to the wrong event does not count.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

_WHITESPACE = re.compile(r"\s+")

# A relation whose direction is inverted is a distinct extraction error, not a
# missing relation.  It is counted separately so a model that learns the
# entities but reverses CAUSES is visible instead of merely scoring low.
CAUSES = "CAUSES"
_GATE_CODES = {"AND", "OR"}


def normalize_name(value: Any) -> str:
    """Fold spacing and width so cosmetic differences do not count as errors."""
    text = unicodedata.normalize("NFKC", str(value or ""))
    return _WHITESPACE.sub("", text).strip()


def asset_id_from_chunk_id(chunk_id: str) -> str:
    """Best-effort document id from a chunk id.

    This is only a fallback for callers that do not have the manifest.  Chunk
    ids are not uniformly ``<asset>-<ordinal>``: a manual chunk can be
    ``grundfos-6859100-f09-c02`` and an asset id itself contains hyphens.
    Prefer :func:`resolve_asset_id` with the manifest's asset list, which
    matches the longest known prefix and is exact.
    """
    value = str(chunk_id or "")
    if "-" not in value:
        return value
    return value.rsplit("-", 1)[0]


def resolve_asset_id(
    chunk_id: str, candidates: Iterable[str]
) -> str | None:
    """Return the longest known asset id that prefixes ``chunk_id``.

    ``candidates`` is the union of the manifest's train/val/test assets.  A
    chunk id matches when it equals the asset or continues with ``-``; the
    longest match wins so ``grundfos-6859100`` is not shadowed by a shorter
    prefix.  ``None`` means the chunk does not belong to any known document.
    """
    value = str(chunk_id or "")
    matches = [
        str(asset)
        for asset in candidates
        if value == str(asset) or value.startswith(f"{asset}-")
    ]
    return max(matches, key=len) if matches else None


def source_group(asset_id: str) -> str:
    """Coarse report group: the cross-domain corpus versus the manuals.

    The W7 experiment holds out one Grundfos manual while training on two
    others, so the manual group is the one that answers whether the
    target-domain stage helped.  Unknown prefixes stay visible as ``other``
    rather than being folded into either side.
    """
    value = str(asset_id or "")
    if value.startswith("CFDK-"):
        return "cfdk"
    if value.startswith("grundfos-") or value.startswith("tianjin-"):
        return "manual"
    return "other"


def _entity_items(target: Mapping[str, Any] | None) -> Iterable[Mapping[str, Any]]:
    if not isinstance(target, Mapping):
        return ()
    entities = target.get("entities")
    if not isinstance(entities, list):
        return ()
    return (item for item in entities if isinstance(item, Mapping))


def _local_names(target: Mapping[str, Any] | None) -> dict[str, str]:
    names: dict[str, str] = {}
    for entity in _entity_items(target):
        local_id = entity.get("local_id")
        if isinstance(local_id, str) and local_id:
            names[local_id] = str(entity.get("name") or local_id)
    return names


def entity_keys(
    target: Mapping[str, Any] | None, *, normalized: bool = False
) -> set[tuple[str, str]]:
    """Return ``(class, name)`` keys; every predicted class is kept.

    A prediction that emits ``Equipment`` is a false positive for this task,
    not something to filter away: filtering would inflate precision.
    """
    keys: set[tuple[str, str]] = set()
    for entity in _entity_items(target):
        class_name = str(entity.get("class") or entity.get("type") or "").strip()
        name = str(entity.get("name") or entity.get("local_id") or "").strip()
        if normalized:
            name = normalize_name(name)
        if class_name and name:
            keys.add((class_name, name))
    return keys


def relation_keys(
    target: Mapping[str, Any] | None, *, normalized: bool = False
) -> set[tuple[str, str, str]]:
    """Return ``(relation_code, source_name, target_name)`` keys."""
    names = _local_names(target)
    keys: set[tuple[str, str, str]] = set()

    def resolve(ref: Any) -> str:
        value = str(ref or "").strip()
        return names.get(value, value)

    if not isinstance(target, Mapping):
        return keys
    relations = target.get("relations")
    if not isinstance(relations, list):
        return keys
    for relation in relations:
        if not isinstance(relation, Mapping):
            continue
        code = str(relation.get("relation_code") or relation.get("relation") or "")
        source = resolve(
            relation.get("source_local_id") or relation.get("source")
        )
        target_ref = resolve(
            relation.get("target_local_id") or relation.get("target")
        )
        if normalized:
            source = normalize_name(source)
            target_ref = normalize_name(target_ref)
        if code and source and target_ref:
            keys.add((code, source, target_ref))
    return keys


def _malformed_entities(target: Mapping[str, Any] | None) -> int:
    count = 0
    for entity in _entity_items(target):
        class_name = str(entity.get("class") or entity.get("type") or "").strip()
        name = str(entity.get("name") or entity.get("local_id") or "").strip()
        if not class_name or not name:
            count += 1
    return count


def _malformed_relations(target: Mapping[str, Any] | None) -> int:
    names = _local_names(target)
    resolvable = set(names) | set(names.values())
    count = 0
    if not isinstance(target, Mapping):
        return 0
    relations = target.get("relations")
    if not isinstance(relations, list):
        return 0
    for relation in relations:
        if not isinstance(relation, Mapping):
            count += 1
            continue
        code = relation.get("relation_code") or relation.get("relation")
        source = relation.get("source_local_id") or relation.get("source")
        target_ref = relation.get("target_local_id") or relation.get("target")
        if not code or not source or not target_ref:
            count += 1
            continue
        if str(source) not in resolvable or str(target_ref) not in resolvable:
            count += 1
    return count


def predicted_quotes(target: Mapping[str, Any] | None) -> list[str]:
    """Collect every evidence quote, including quotes from malformed rows."""
    quotes: list[str] = []
    if not isinstance(target, Mapping):
        return quotes
    for section in ("entities", "relations"):
        items = target.get(section)
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, Mapping):
                continue
            evidence = item.get("evidence")
            if not isinstance(evidence, list):
                continue
            for entry in evidence:
                if not isinstance(entry, Mapping):
                    continue
                quote = entry.get("quote")
                if isinstance(quote, str) and quote:
                    quotes.append(quote)
    return quotes


def predicted_names(target: Mapping[str, Any] | None) -> list[str]:
    return [
        str(entity.get("name") or "").strip()
        for entity in _entity_items(target)
        if str(entity.get("name") or "").strip()
    ]


def _canonical_expression(
    expression: Any, names: Mapping[str, str]
) -> tuple[Any, ...] | None:
    """Canonicalize a cause_rule tree, resolving local ids to names.

    AND/OR are commutative, so child order is sorted out of the comparison;
    a different grouping still compares unequal.  An expression that cannot
    be resolved returns ``None`` and is not counted as a rule prediction.
    """
    if not isinstance(expression, Mapping):
        return None
    event_id = expression.get("event_local_id")
    if isinstance(event_id, str) and event_id:
        return ("event", normalize_name(names.get(event_id, event_id)))
    op = expression.get("op")
    children = expression.get("children")
    if op not in _GATE_CODES or not isinstance(children, list) or len(children) < 2:
        return None
    canonical_children: list[tuple[Any, ...]] = []
    for child in children:
        canonical = _canonical_expression(child, names)
        if canonical is None:
            return None
        canonical_children.append(canonical)
    return (op, tuple(sorted(canonical_children, key=repr)))


def _cause_rules(target: Mapping[str, Any] | None) -> dict[str, tuple[Any, ...]]:
    names = _local_names(target)
    rules: dict[str, tuple[Any, ...]] = {}
    for entity in _entity_items(target):
        local_id = entity.get("local_id")
        rule = entity.get("cause_rule")
        if not isinstance(local_id, str) or not local_id:
            continue
        if not isinstance(rule, Mapping):
            continue
        canonical = _canonical_expression(rule, names)
        if canonical is None:
            continue
        rules[normalize_name(names.get(local_id, local_id))] = canonical
    return rules


@dataclass(frozen=True)
class SampleScore:
    """Per-sample counts.  Aggregation is the only place rates are computed."""

    sample_id: str
    asset_id: str
    group: str
    json_valid: bool
    contract_valid: bool
    contract_error: str | None
    generation_error: str | None
    entity_tp: int
    entity_pred: int
    entity_gold: int
    entity_tp_exact: int
    entity_pred_exact: int
    entity_gold_exact: int
    relation_tp: int
    relation_pred: int
    relation_gold: int
    relation_pred_exact: int
    relation_gold_exact: int
    relation_tp_exact: int
    quotes: int
    quotes_verbatim: int
    empty_pred: bool
    empty_gold: bool
    output_tokens: int
    name_pred: int
    name_grounded: int
    cause_rule_total: int
    cause_rule_correct: int
    reversed_relations: int
    malformed_entities: int
    malformed_relations: int


def score_sample(
    *,
    sample_id: str,
    chunk_text: str,
    gold: Mapping[str, Any] | None,
    prediction: Mapping[str, Any] | None,
    json_valid: bool = True,
    contract_valid: bool = False,
    contract_error: str | None = None,
    generation_error: str | None = None,
    output_tokens: int = 0,
    asset_id: str | None = None,
) -> SampleScore:
    """Score one prediction against its gold target.

    ``prediction`` may be ``None`` only when ``json_valid`` is false; the
    sample still contributes to the denominator, so a broken model cannot
    improve its score by outputting nothing parseable.
    """
    resolved_asset = asset_id or asset_id_from_chunk_id(sample_id)
    entity_pred = entity_keys(prediction)
    entity_gold = entity_keys(gold)
    entity_pred_norm = entity_keys(prediction, normalized=True)
    entity_gold_norm = entity_keys(gold, normalized=True)
    relation_pred = relation_keys(prediction)
    relation_gold = relation_keys(gold)
    relation_pred_norm = relation_keys(prediction, normalized=True)
    relation_gold_norm = relation_keys(gold, normalized=True)

    quotes = predicted_quotes(prediction)
    names = {normalize_name(name) for name in predicted_names(prediction)}
    normalized_chunk = normalize_name(chunk_text)
    quote_set = set(quotes)
    verbatim = sum(1 for quote in quote_set if quote in chunk_text)

    gold_rules = _cause_rules(gold)
    predicted_rules = _cause_rules(prediction)
    cause_rule_correct = sum(
        1
        for name, rule in gold_rules.items()
        if predicted_rules.get(name) == rule
    )

    reversed_relations = 0
    for code, source, target in relation_pred_norm:
        if code != CAUSES:
            continue
        if (
            (code, target, source) in relation_gold_norm
            and (code, source, target) not in relation_gold_norm
        ):
            reversed_relations += 1

    return SampleScore(
        sample_id=sample_id,
        asset_id=resolved_asset,
        group=source_group(resolved_asset),
        json_valid=bool(json_valid),
        contract_valid=bool(contract_valid),
        contract_error=contract_error,
        generation_error=generation_error,
        entity_tp=len(entity_pred_norm & entity_gold_norm),
        entity_pred=len(entity_pred_norm),
        entity_gold=len(entity_gold_norm),
        entity_pred_exact=len(entity_pred),
        entity_gold_exact=len(entity_gold),
        entity_tp_exact=len(entity_pred & entity_gold),
        relation_tp=len(relation_pred_norm & relation_gold_norm),
        relation_pred=len(relation_pred_norm),
        relation_gold=len(relation_gold_norm),
        relation_pred_exact=len(relation_pred),
        relation_gold_exact=len(relation_gold),
        relation_tp_exact=len(relation_pred & relation_gold),
        quotes=len(quote_set),
        quotes_verbatim=verbatim,
        empty_pred=not entity_pred_norm and not relation_pred_norm,
        empty_gold=not entity_gold_norm and not relation_gold_norm,
        output_tokens=int(output_tokens),
        name_pred=len(names),
        name_grounded=sum(1 for name in names if name in normalized_chunk),
        cause_rule_total=len(gold_rules),
        cause_rule_correct=cause_rule_correct,
        reversed_relations=reversed_relations,
        malformed_entities=_malformed_entities(prediction),
        malformed_relations=_malformed_relations(prediction),
    )


def _prf(tp: int, predicted: int, gold: int) -> dict[str, Any]:
    precision = tp / predicted if predicted else 1.0
    recall = tp / gold if gold else 1.0
    f1 = (
        2 * precision * recall / (precision + recall)
        if precision + recall
        else 0.0
    )
    return {
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "f1": round(f1, 4),
        "tp": tp,
        "predicted": predicted,
        "gold": gold,
    }


def aggregate_scores(scores: Sequence[SampleScore]) -> dict[str, Any]:
    """Micro-average counts over samples, plus failure and grounding rates."""
    total = len(scores)
    if total == 0:
        return {"samples": 0}

    def rate(numerator: int, denominator: int) -> float | None:
        return round(numerator / denominator, 4) if denominator else None

    quotes = sum(score.quotes for score in scores)
    quotes_verbatim = sum(score.quotes_verbatim for score in scores)
    entity_pred = sum(score.entity_pred for score in scores)
    relation_pred = sum(score.relation_pred for score in scores)
    negative = [score for score in scores if score.empty_gold]

    return {
        "samples": total,
        "json_valid_rate": rate(
            sum(score.json_valid for score in scores), total
        ),
        "contract_valid_rate": rate(
            sum(score.contract_valid for score in scores), total
        ),
        "entity": _prf(
            sum(score.entity_tp for score in scores),
            entity_pred,
            sum(score.entity_gold for score in scores),
        ),
        "entity_exact": _prf(
            sum(score.entity_tp_exact for score in scores),
            sum(score.entity_pred_exact for score in scores),
            sum(score.entity_gold_exact for score in scores),
        ),
        "relation": _prf(
            sum(score.relation_tp for score in scores),
            relation_pred,
            sum(score.relation_gold for score in scores),
        ),
        "relation_exact": _prf(
            sum(score.relation_tp_exact for score in scores),
            sum(score.relation_pred_exact for score in scores),
            sum(score.relation_gold_exact for score in scores),
        ),
        "quote_verbatim_rate": rate(quotes_verbatim, quotes),
        "quotes": quotes,
        "quotes_verbatim": quotes_verbatim,
        "empty_pred_rate": rate(
            sum(score.empty_pred for score in scores), total
        ),
        "empty_gold_rate": rate(
            sum(score.empty_gold for score in scores), total
        ),
        "negative_accuracy": rate(
            sum(score.empty_pred for score in negative), len(negative)
        ),
        "avg_output_tokens": round(
            sum(score.output_tokens for score in scores) / total, 2
        ),
        "entity_name_grounded_rate": rate(
            sum(score.name_grounded for score in scores),
            sum(score.name_pred for score in scores),
        ),
        "cause_rule_accuracy": rate(
            sum(score.cause_rule_correct for score in scores),
            sum(score.cause_rule_total for score in scores),
        ),
        "cause_rules": sum(score.cause_rule_total for score in scores),
        "reversed_relations": sum(
            score.reversed_relations for score in scores
        ),
        "malformed_entities": sum(
            score.malformed_entities for score in scores
        ),
        "malformed_relations": sum(
            score.malformed_relations for score in scores
        ),
        "generation_errors": sum(
            bool(score.generation_error) for score in scores
        ),
        "json_errors": sum(not score.json_valid for score in scores),
        "contract_errors": sum(
            score.json_valid and not score.contract_valid for score in scores
        ),
    }


def group_scores(
    scores: Sequence[SampleScore], *, key: str = "group"
) -> dict[str, dict[str, Any]]:
    """Aggregate by ``group`` or ``asset_id``, sorted for stable reports."""
    buckets: dict[str, list[SampleScore]] = {}
    for score in scores:
        buckets.setdefault(str(getattr(score, key)), []).append(score)
    return {
        name: aggregate_scores(bucket)
        for name, bucket in sorted(buckets.items())
    }


def check_split_leakage(
    manifest: Mapping[str, Any], chunk_ids: Iterable[str]
) -> list[str]:
    """Return human-readable leakage problems, empty when the split is clean.

    The manifest is the builder's own record of which documents went where.
    The evaluation set must not reuse a training document, and every evaluated
    chunk should belong to a document the manifest assigned to ``test``.
    Missing ``assets`` data is a problem: it means the check could not run.
    """
    assets = manifest.get("assets")
    if not isinstance(assets, Mapping):
        return ["manifest has no assets object; cannot verify split leakage"]
    train = set(assets.get("train") or [])
    val = set(assets.get("val") or [])
    test = set(assets.get("test") or [])
    known = train | val | test
    problems: list[str] = []
    for left_name, left, right_name, right in (
        ("train", train, "val", val),
        ("train", train, "test", test),
        ("val", val, "test", test),
    ):
        overlap = sorted(left & right)
        if overlap:
            problems.append(
                f"{left_name}/{right_name} asset overlap: {overlap}"
            )
    for chunk_id in chunk_ids:
        asset_id = resolve_asset_id(chunk_id, known)
        if asset_id is None:
            problems.append(
                f"{chunk_id}: cannot match any asset in the manifest"
            )
        elif asset_id in train:
            problems.append(f"{chunk_id}: asset {asset_id} is in train")
        elif test and asset_id not in test:
            problems.append(
                f"{chunk_id}: asset {asset_id} is not in the manifest test split"
            )
    return problems
