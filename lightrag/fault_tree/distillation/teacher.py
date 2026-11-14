"""OpenAI-compatible teacher client and output normalizer for W7.

The teacher is an external API.  This module does not decide what may be sent;
the CLI enforces the ``sources.yml`` public gate before calling it.  Its job is
to build a deterministic request, parse the response without trusting it, and
narrow it to the student's ``FaultEvent + CAUSES`` contract.

Only the pure helpers are imported by tests with fake payloads; no live API is
required.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from lightrag.fault_tree.distillation.labels import (
    StudentLabelError,
    validate_student_target,
)


class TeacherError(RuntimeError):
    """The teacher call failed or returned an unusable payload."""


class TeacherOutputError(TeacherError):
    """The teacher returned JSON that cannot be narrowed to a student target."""


@dataclass(frozen=True)
class TeacherConfig:
    endpoint: str
    api_key: str
    model: str = "deepseek-flash"
    timeout: float = 120.0
    max_output_tokens: int = 2048
    max_attempts: int = 3
    json_mode: bool = True
    concurrency: int = 2

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> "TeacherConfig":
        endpoint = str(env.get("W7_TEACHER_ENDPOINT") or "").strip()
        api_key = str(env.get("W7_TEACHER_API_KEY") or "").strip()
        if not endpoint:
            raise TeacherError("W7_TEACHER_ENDPOINT is not set")
        if not api_key:
            raise TeacherError("W7_TEACHER_API_KEY is not set")
        try:
            timeout = float(env.get("W7_TEACHER_TIMEOUT") or 120)
            max_output_tokens = int(
                env.get("W7_TEACHER_MAX_OUTPUT_TOKENS") or 2048
            )
            max_attempts = int(env.get("W7_TEACHER_MAX_ATTEMPTS") or 3)
            concurrency = int(env.get("W7_TEACHER_MAX_CONCURRENCY") or 2)
        except ValueError as exc:
            raise TeacherError(f"invalid teacher numeric setting: {exc}") from exc
        return cls(
            endpoint=endpoint,
            api_key=api_key,
            model=str(env.get("W7_TEACHER_MODEL") or "deepseek-flash").strip(),
            timeout=timeout,
            max_output_tokens=max_output_tokens,
            max_attempts=max(1, max_attempts),
            json_mode=_as_bool(env.get("W7_TEACHER_JSON_MODE"), True),
            concurrency=max(1, concurrency),
        )


def _as_bool(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def load_env_file(path: Path) -> dict[str, str]:
    """Read a minimal ``KEY=VALUE`` file; never requires python-dotenv."""
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise TeacherError(f"cannot read {path}: {exc}") from exc
    for line_no, line in enumerate(lines, start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "=" not in stripped:
            raise TeacherError(f"{path}:{line_no}: expected KEY=VALUE")
        key, raw = stripped.split("=", 1)
        key = key.strip()
        value = raw.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        if key:
            values[key] = value
    return values


@dataclass(frozen=True)
class TeacherCall:
    content: str
    usage: dict[str, Any]
    prompt_sha256: str
    response_sha256: str


def build_payload(
    config: TeacherConfig, system_prompt: str, user_prompt: str
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": config.model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0,
        "max_tokens": config.max_output_tokens,
    }
    if config.json_mode:
        payload["response_format"] = {"type": "json_object"}
    return payload


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def parse_chat_completion(payload: Mapping[str, Any]) -> TeacherCall:
    try:
        choices = payload["choices"]
        message = choices[0]["message"]
        content = message["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise TeacherError("teacher response has no choices[0].message.content") from exc
    if not isinstance(content, str) or not content.strip():
        raise TeacherError("teacher returned empty content")
    usage = payload.get("usage")
    return TeacherCall(
        content=content,
        usage=dict(usage) if isinstance(usage, dict) else {},
        prompt_sha256="",
        response_sha256=_sha256(content),
    )


def extract_json_object(text: str) -> dict[str, Any]:
    """Return the first JSON object, tolerating Markdown fences and prose."""
    stripped = text.strip()
    if not stripped:
        raise TeacherOutputError("empty teacher output")
    try:
        whole = json.loads(stripped)
    except json.JSONDecodeError:
        whole = None
    if isinstance(whole, dict):
        return whole
    if whole is not None:
        raise TeacherOutputError("teacher output is JSON but not an object")

    start = stripped.find("{")
    if start < 0:
        raise TeacherOutputError("teacher output contains no JSON object")
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(stripped)):
        char = stripped[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                candidate = stripped[start : index + 1]
                try:
                    parsed = json.loads(candidate)
                except json.JSONDecodeError as exc:
                    raise TeacherOutputError(
                        f"teacher JSON is invalid: {exc}"
                    ) from exc
                if not isinstance(parsed, dict):
                    raise TeacherOutputError(
                        "teacher output is not a JSON object"
                    )
                return parsed
    raise TeacherOutputError("teacher JSON object is not closed")


def _grounded_quotes(raw: Any, chunk_text: str) -> list[str]:
    if not isinstance(raw, list):
        return []
    quotes: list[str] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        quote = item.get("quote")
        if isinstance(quote, str) and quote and quote in chunk_text:
            if quote not in quotes:
                quotes.append(quote)
    return quotes


def _grounded_aliases(raw: Any, chunk_text: str) -> list[str]:
    if not isinstance(raw, list):
        return []
    aliases: list[str] = []
    for alias in raw:
        if (
            isinstance(alias, str)
            and alias
            and alias in chunk_text
            and alias not in aliases
        ):
            aliases.append(alias)
    return aliases


def _prune_expression(
    expression: Any, valid_ids: set[str]
) -> dict[str, Any] | None:
    if not isinstance(expression, dict):
        return None
    event_id = expression.get("event_local_id")
    if isinstance(event_id, str) and event_id:
        return {"event_local_id": event_id} if event_id in valid_ids else None
    op = expression.get("op")
    children = expression.get("children")
    if op not in {"AND", "OR"} or not isinstance(children, list):
        return None
    kept = [
        child
        for child in (_prune_expression(item, valid_ids) for item in children)
        if child is not None
    ]
    if len(kept) < 2:
        return None
    return {"op": op, "children": kept}


def normalize_teacher_target(
    raw: dict[str, Any], chunk_text: str
) -> tuple[dict[str, Any], list[str]]:
    """Narrow a teacher payload to a contract-valid student target.

    Raises :class:`TeacherOutputError` when the payload is structurally wrong
    or when the teacher named FaultEvents but every one lost its evidence.
    Returning an empty target is reserved for a genuinely empty extraction, so
    a broken output is never silently trained as a negative.
    """
    if not isinstance(raw, dict):
        raise TeacherOutputError("teacher output is not an object")
    raw_entities = raw.get("entities")
    raw_relations = raw.get("relations")
    if not isinstance(raw_entities, list) or not isinstance(raw_relations, list):
        raise TeacherOutputError(
            "teacher output must contain entities and relations arrays"
        )
    warnings: list[str] = []
    entities: dict[str, dict[str, Any]] = {}
    dropped = 0
    for raw_entity in raw_entities:
        if not isinstance(raw_entity, dict):
            dropped += 1
            continue
        class_code = raw_entity.get("class") or raw_entity.get("type")
        if class_code != "FaultEvent":
            continue
        local_id = str(raw_entity.get("local_id") or "").strip()
        if not local_id or local_id in entities:
            dropped += 1
            continue
        name = str(raw_entity.get("name") or "").strip()
        if not name:
            dropped += 1
            continue
        quotes = _grounded_quotes(raw_entity.get("evidence"), chunk_text)
        if not quotes and name in chunk_text:
            quotes = [name]
            warnings.append(f"{local_id}:evidence-from-name")
        if not quotes:
            dropped += 1
            continue
        entity: dict[str, Any] = {
            "local_id": local_id,
            "class": "FaultEvent",
            "name": name,
            "description": str(
                raw_entity.get("description") or f"故障事件：{name}"
            ),
            "evidence": [{"quote": quote} for quote in quotes],
        }
        aliases = _grounded_aliases(raw_entity.get("aliases"), chunk_text)
        if aliases:
            entity["aliases"] = aliases
        if isinstance(raw_entity.get("cause_rule"), dict):
            entity["cause_rule"] = raw_entity["cause_rule"]
        entities[local_id] = entity

    if dropped:
        warnings.append(f"dropped-entities:{dropped}")
    if raw_entities and not entities:
        raise TeacherOutputError("every teacher entity was dropped")
    if raw_relations and not entities:
        raise TeacherOutputError("teacher relations have no surviving entities")

    relations: list[dict[str, Any]] = []
    for raw_relation in raw_relations:
        if not isinstance(raw_relation, dict):
            warnings.append("dropped-relation:not-an-object")
            continue
        if raw_relation.get("relation_code") != "CAUSES":
            continue
        source = str(raw_relation.get("source_local_id") or "").strip()
        target = str(raw_relation.get("target_local_id") or "").strip()
        if source not in entities or target not in entities:
            warnings.append("dropped-relation:endpoint-missing")
            continue
        quotes = _grounded_quotes(raw_relation.get("evidence"), chunk_text)
        if not quotes:
            warnings.append("dropped-relation:evidence-missing")
            continue
        relation: dict[str, Any] = {
            "relation_code": "CAUSES",
            "source_local_id": source,
            "target_local_id": target,
            "evidence": [{"quote": quote} for quote in quotes],
        }
        gate_type = raw_relation.get("gate_type_hint")
        if gate_type in {"AND", "OR"}:
            relation["gate_type_hint"] = gate_type
        gate_key = raw_relation.get("gate_key")
        if isinstance(gate_key, str) and gate_key:
            relation["gate_key"] = gate_key
        relations.append(relation)

    valid_ids = set(entities)
    for local_id, entity in list(entities.items()):
        if "cause_rule" not in entity:
            continue
        pruned = _prune_expression(entity["cause_rule"], valid_ids)
        if pruned is None:
            entity.pop("cause_rule", None)
            warnings.append(f"{local_id}:cause-rule-pruned")
        else:
            entity["cause_rule"] = pruned

    target = {
        "entities": list(entities.values()),
        "relations": relations,
    }
    try:
        validate_student_target(target, chunk_text, sample_id="teacher")
    except StudentLabelError as exc:
        raise TeacherOutputError(str(exc)) from exc
    return target, warnings


class TeacherClient:
    """Async OpenAI-compatible client with retries and a concurrency limit."""

    def __init__(self, config: TeacherConfig, *, transport: Any = None):
        try:
            import httpx
        except ImportError as exc:  # pragma: no cover - environment guard
            raise TeacherError(
                "httpx is required for teacher labelling"
            ) from exc
        self._httpx = httpx
        self._config = config
        self._semaphore = asyncio.Semaphore(config.concurrency)
        self._client = httpx.AsyncClient(
            timeout=config.timeout, transport=transport
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def label(
        self, system_prompt: str, user_prompt: str
    ) -> TeacherCall:
        payload = build_payload(self._config, system_prompt, user_prompt)
        url = f"{self._config.endpoint.rstrip('/')}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self._config.api_key}",
            "Content-Type": "application/json",
        }
        last_error: Exception | None = None
        async with self._semaphore:
            for attempt in range(1, self._config.max_attempts + 1):
                try:
                    response = await self._client.post(
                        url, headers=headers, json=payload
                    )
                    if response.status_code in {429, 500, 502, 503, 504}:
                        raise self._httpx.HTTPStatusError(
                            f"retryable status {response.status_code}",
                            request=response.request,
                            response=response,
                        )
                    response.raise_for_status()
                    call = parse_chat_completion(response.json())
                    return TeacherCall(
                        content=call.content,
                        usage=call.usage,
                        prompt_sha256=_sha256(system_prompt + "\n" + user_prompt),
                        response_sha256=call.response_sha256,
                    )
                except self._httpx.HTTPStatusError as exc:
                    last_error = exc
                    retryable = exc.response.status_code in {
                        429,
                        500,
                        502,
                        503,
                        504,
                    }
                    if not retryable or attempt >= self._config.max_attempts:
                        break
                    await asyncio.sleep(min(2 ** attempt, 30))
                except (
                    self._httpx.HTTPError,
                    ValueError,
                    TeacherError,
                ) as exc:
                    last_error = exc
                    if attempt >= self._config.max_attempts:
                        break
                    await asyncio.sleep(min(2 ** attempt, 30))
        raise TeacherError(str(last_error) if last_error else "teacher call failed")
