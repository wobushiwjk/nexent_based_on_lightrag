"""Score an extraction model on the held-out seed chunk.

The script talks to any OpenAI-compatible chat endpoint, so the same command
works for the base model, the merged model, and a quantized GGUF served by
Ollama::

    python evaluate.py --model qwen2.5:3b-instruct
    python evaluate.py --model industrial-extractor

It reports JSON validity, entity/relation F1, and the fraction of predicted
quotes that appear verbatim in the source chunk.  With the five-sample seed
set these numbers are a pipeline check, not a quality claim; replace the data
with teacher-labelled chunks before reading them as model quality.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
DEFAULT_ENDPOINT = "http://localhost:11434/v1/chat/completions"


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def _extract_json(text: str) -> dict[str, Any]:
    """Pull the first JSON object out of a model reply."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```[A-Za-z0-9_-]*\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    start = cleaned.find("{")
    if start < 0:
        raise ValueError("no JSON object in reply")
    decoder = json.JSONDecoder()
    try:
        payload, _ = decoder.raw_decode(cleaned[start:])
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("reply JSON must be an object")
    return payload


def _chat(
    endpoint: str,
    model: str,
    api_key: str,
    messages: list[dict[str, str]],
    *,
    temperature: float,
    max_tokens: int,
    timeout: float,
) -> str:
    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": False,
    }
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    request = urllib.request.Request(
        endpoint,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code}: {detail[:500]}") from exc
    data = json.loads(body)
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError(f"unexpected response: {body[:500]}") from exc
    if isinstance(content, list):
        content = "".join(
            part.get("text", "")
            for part in content
            if isinstance(part, dict)
        )
    return str(content)


def _entity_keys(target: dict[str, Any]) -> set[tuple[str, str]]:
    keys: set[tuple[str, str]] = set()
    for entity in target.get("entities") or []:
        if not isinstance(entity, dict):
            continue
        class_name = str(entity.get("class") or entity.get("type") or "")
        if class_name != "FaultEvent":
            continue
        name = str(entity.get("name") or entity.get("local_id") or "").strip()
        if name:
            keys.add((class_name, name))
    return keys


def _relation_keys(target: dict[str, Any]) -> set[tuple[str, str, str]]:
    names = {
        str(entity.get("local_id")): str(entity.get("name") or "").strip()
        for entity in target.get("entities") or []
        if isinstance(entity, dict) and entity.get("local_id")
    }

    def resolve(ref: Any) -> str:
        value = str(ref or "").strip()
        return names.get(value) or value

    keys: set[tuple[str, str, str]] = set()
    for relation in target.get("relations") or []:
        if not isinstance(relation, dict):
            continue
        code = str(relation.get("relation_code") or "")
        source = resolve(
            relation.get("source_local_id") or relation.get("source")
        )
        target_ref = resolve(
            relation.get("target_local_id") or relation.get("target")
        )
        if code and source and target_ref:
            keys.add((code, source, target_ref))
    return keys


def _predicted_quotes(target: dict[str, Any]) -> list[str]:
    quotes: list[str] = []
    for entity in target.get("entities") or []:
        if not isinstance(entity, dict):
            continue
        for item in entity.get("evidence") or []:
            if isinstance(item, dict) and item.get("quote"):
                quotes.append(str(item["quote"]))
    for relation in target.get("relations") or []:
        if not isinstance(relation, dict):
            continue
        for item in relation.get("evidence") or []:
            if isinstance(item, dict) and item.get("quote"):
                quotes.append(str(item["quote"]))
    return quotes


def _prf(
    tp: int, predicted: int, gold: int
) -> tuple[float, float, float]:
    precision = tp / predicted if predicted else 1.0
    recall = tp / gold if gold else 1.0
    f1 = (
        2 * precision * recall / (precision + recall)
        if precision + recall
        else 0.0
    )
    return precision, recall, f1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--endpoint",
        default=os.environ.get("EXTRACTION_ENDPOINT", DEFAULT_ENDPOINT),
    )
    parser.add_argument("--model", default="industrial-extractor")
    parser.add_argument("--api-key", default=os.environ.get("OPENAI_API_KEY", ""))
    parser.add_argument("--data", type=Path, default=HERE / "data" / "val.jsonl")
    parser.add_argument(
        "--meta", type=Path, default=HERE / "data" / "val_meta.jsonl"
    )
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument("--max-tokens", type=int, default=2048)
    parser.add_argument("--timeout", type=float, default=300.0)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)

    rows = _read_jsonl(args.data)
    meta = _read_jsonl(args.meta)
    if len(rows) != len(meta):
        raise SystemExit(
            f"{args.data} has {len(rows)} rows but {args.meta} has {len(meta)}"
        )
    if args.limit:
        rows = rows[: args.limit]
        meta = meta[: args.limit]

    totals = {
        "json_valid": 0,
        "entity_tp": 0,
        "entity_pred": 0,
        "entity_gold": 0,
        "relation_tp": 0,
        "relation_pred": 0,
        "relation_gold": 0,
        "quotes": 0,
        "quotes_verbatim": 0,
    }
    report: dict[str, Any] = {"endpoint": args.endpoint, "model": args.model, "samples": []}

    for index, (row, item) in enumerate(zip(rows, meta, strict=True), start=1):
        sample_id = str(item.get("id") or row.get("id") or index)
        chunk_text = str(item.get("chunk_text") or "")
        gold = item.get("gold") or {}
        messages = []
        system = row.get("system")
        if system:
            messages.append({"role": "system", "content": str(system)})
        messages.append(
            {
                "role": "user",
                "content": str(row["conversations"][0]["value"]),
            }
        )

        error: str | None = None
        prediction: dict[str, Any] = {"entities": [], "relations": []}
        try:
            raw = _chat(
                args.endpoint,
                args.model,
                args.api_key,
                messages,
                temperature=args.temperature,
                max_tokens=args.max_tokens,
                timeout=args.timeout,
            )
            prediction = _extract_json(raw)
            totals["json_valid"] += 1
        except (ValueError, RuntimeError, urllib.error.URLError) as exc:
            error = str(exc)

        entity_pred = _entity_keys(prediction)
        entity_gold = _entity_keys(gold)
        relation_pred = _relation_keys(prediction)
        relation_gold = _relation_keys(gold)
        entity_tp = len(entity_pred & entity_gold)
        relation_tp = len(relation_pred & relation_gold)

        quotes = _predicted_quotes(prediction)
        verbatim = sum(1 for quote in quotes if quote in chunk_text)

        totals["entity_tp"] += entity_tp
        totals["entity_pred"] += len(entity_pred)
        totals["entity_gold"] += len(entity_gold)
        totals["relation_tp"] += relation_tp
        totals["relation_pred"] += len(relation_pred)
        totals["relation_gold"] += len(relation_gold)
        totals["quotes"] += len(quotes)
        totals["quotes_verbatim"] += verbatim

        entity_p, entity_r, entity_f1 = _prf(
            entity_tp, len(entity_pred), len(entity_gold)
        )
        relation_p, relation_r, relation_f1 = _prf(
            relation_tp, len(relation_pred), len(relation_gold)
        )
        sample_report = {
            "id": sample_id,
            "json_valid": error is None,
            "error": error,
            "entity_p": round(entity_p, 4),
            "entity_r": round(entity_r, 4),
            "entity_f1": round(entity_f1, 4),
            "relation_p": round(relation_p, 4),
            "relation_r": round(relation_r, 4),
            "relation_f1": round(relation_f1, 4),
            "quotes": len(quotes),
            "quotes_verbatim": verbatim,
            "prediction": prediction if error is None else None,
        }
        report["samples"].append(sample_report)
        print(
            f"[{index}/{len(rows)}] {sample_id}: "
            f"json={'ok' if error is None else 'bad'} "
            f"ent_f1={entity_f1:.2f} rel_f1={relation_f1:.2f} "
            f"quote={verbatim}/{len(quotes)}",
            file=sys.stderr,
        )

    entity_p, entity_r, entity_f1 = _prf(
        totals["entity_tp"], totals["entity_pred"], totals["entity_gold"]
    )
    relation_p, relation_r, relation_f1 = _prf(
        totals["relation_tp"], totals["relation_pred"], totals["relation_gold"]
    )
    report["aggregate"] = {
        "json_valid_rate": round(totals["json_valid"] / len(rows), 4)
        if rows
        else 0.0,
        "entity_p": round(entity_p, 4),
        "entity_r": round(entity_r, 4),
        "entity_f1": round(entity_f1, 4),
        "relation_p": round(relation_p, 4),
        "relation_r": round(relation_r, 4),
        "relation_f1": round(relation_f1, 4),
        "quotes": totals["quotes"],
        "quotes_verbatim": totals["quotes_verbatim"],
        "quote_verbatim_rate": round(
            totals["quotes_verbatim"] / totals["quotes"], 4
        )
        if totals["quotes"]
        else None,
    }
    output = json.dumps(report, ensure_ascii=False, indent=2)
    print(output)
    if args.out is not None:
        args.out.write_text(output + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
