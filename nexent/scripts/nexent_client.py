"""Minimal Nexent HTTP client used by the import and demo scripts.

Nexent v2.8.0 exposes three services this package talks to:

* the configuration service (default ``http://localhost:5010``) for identity,
  MCP registration, tools, Skills, knowledge bases, models and agent import;
* the northbound service (default ``http://localhost:5013``) for
  ``/nb/v1/chat/run``, authenticated with an API key instead of a JWT;
* nothing else — the runtime service is driven by the configuration service.

Every route below was checked against the v2.8.0 deployment; the paths are
relative to the service root (the configuration service also accepts them
under ``/api``, which is how its web frontend proxies them).

Design constraints:

* standard library only (``urllib``), so the script runs in the project venv
  without adding an HTTP dependency;
* the transport is injectable, so tests exercise request construction and
  response handling without a live Nexent;
* errors carry the parsed body, because Nexent reports policy failures (a
  duplicate MCP name, an unresolved Skill conflict) as structured 4xx bodies
  that the import flow must branch on.
"""

from __future__ import annotations

import json
import socket
import ssl
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Mapping, Sequence

DEFAULT_CONFIG_URL = "http://localhost:5010"
DEFAULT_NORTHBOUND_URL = "http://localhost:5013"
DEFAULT_MCP_NAME = "industrial-fault-domain"

# Fields whose values are secrets and must never be printed by a report.
_SECRET_KEYS = frozenset(
    {"access_key", "password", "authorization_token", "custom_headers", "api_key"}
)


@dataclass(frozen=True)
class HttpResponse:
    """One HTTP response, independent of the transport used."""

    status: int
    headers: Mapping[str, str]
    body: bytes

    def json(self) -> Any:
        if not self.body:
            return None
        try:
            return json.loads(self.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None

    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")


class NexentError(RuntimeError):
    """A Nexent request failed or returned an unexpected payload."""

    def __init__(self, method: str, url: str, status: int, body: str):
        self.method = method
        self.url = url
        self.status = status
        self.body = body
        super().__init__(f"{method} {url} -> HTTP {status}: {body[:500]}")

    @property
    def payload(self) -> Any:
        try:
            return json.loads(self.body)
        except (TypeError, json.JSONDecodeError):
            return None

    @property
    def detail(self) -> str:
        payload = self.payload
        if isinstance(payload, dict):
            detail = payload.get("detail", payload.get("message"))
            if isinstance(detail, str):
                return detail
            if detail is not None:
                return json.dumps(detail, ensure_ascii=False)
        return self.body[:500]


class NexentConfigError(RuntimeError):
    """The running Nexent lacks an artifact the import flow requires."""


Transport = Callable[[str, str, Mapping[str, str], bytes | None], HttpResponse]
StreamTransport = Callable[[str, str, Mapping[str, str], bytes | None], Iterable[bytes]]


def _normalize_base(url: str) -> str:
    return url.rstrip("/")


class UrllibTransport:
    """Default transport built on ``urllib.request``."""

    def __init__(self, timeout: float = 60.0) -> None:
        self.timeout = timeout

    def __call__(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: bytes | None,
    ) -> HttpResponse:
        request = urllib.request.Request(
            url, data=body, headers=dict(headers), method=method
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return HttpResponse(
                    status=response.status,
                    headers=dict(response.headers.items()),
                    body=response.read(),
                )
        except urllib.error.HTTPError as exc:
            return HttpResponse(
                status=exc.code,
                headers=dict(exc.headers.items()) if exc.headers else {},
                body=exc.read(),
            )
        except urllib.error.URLError as exc:
            reason = exc.reason
            if isinstance(reason, (socket.timeout, TimeoutError)):
                raise NexentError(method, url, 0, f"timeout: {reason}") from exc
            if isinstance(reason, ssl.SSLError):
                raise NexentError(method, url, 0, f"TLS error: {reason}") from exc
            raise NexentError(method, url, 0, f"connection failed: {reason}") from exc

    def stream(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: bytes | None,
    ) -> Iterator[bytes]:
        request = urllib.request.Request(
            url, data=body, headers=dict(headers), method=method
        )
        try:
            response = urllib.request.urlopen(request, timeout=self.timeout)
        except urllib.error.HTTPError as exc:
            raise NexentError(
                method, url, exc.code, exc.read().decode("utf-8", "replace")
            ) from exc
        except urllib.error.URLError as exc:
            raise NexentError(
                method, url, 0, f"connection failed: {exc.reason}"
            ) from exc
        with response:
            for line in response:
                yield line


def encode_multipart(
    fields: Sequence[tuple[str, str]],
    files: Sequence[tuple[str, str, bytes, str]],
) -> tuple[bytes, str]:
    """Encode a multipart/form-data body.

    ``files`` entries are ``(field_name, filename, content, content_type)``.
    The boundary is derived from the field/file names, so two calls with the
    same inputs produce identical bytes (tests rely on that).
    """
    boundary = "----lightrag-nexent-boundary"
    existing = [value for _, value in fields] + [name for _, name, _, _ in files]
    while any(boundary in value for value in existing):
        boundary += "-x"

    chunks: list[bytes] = []
    for name, value in fields:
        chunks.append(f"--{boundary}\r\n".encode("utf-8"))
        chunks.append(
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("utf-8")
        )
        chunks.append(value.encode("utf-8"))
        chunks.append(b"\r\n")
    for field_name, filename, content, content_type in files:
        chunks.append(f"--{boundary}\r\n".encode("utf-8"))
        chunks.append(
            (
                f'Content-Disposition: form-data; name="{field_name}"; '
                f'filename="{filename}"\r\n'
            ).encode("utf-8")
        )
        chunks.append(f"Content-Type: {content_type}\r\n\r\n".encode("utf-8"))
        chunks.append(content)
        chunks.append(b"\r\n")
    chunks.append(f"--{boundary}--\r\n".encode("utf-8"))
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


def parse_sse_events(lines: Iterable[bytes | str]) -> Iterator[dict[str, Any]]:
    """Yield the JSON payload of each ``data:`` line in a server-sent stream."""
    for raw in lines:
        line = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
        line = line.rstrip("\r\n")
        if not line.startswith("data:"):
            continue
        payload = line[len("data:") :].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            event = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            yield event


def redact(value: Any) -> Any:
    """Return ``value`` with secret-looking keys masked, for reports."""
    if isinstance(value, dict):
        return {
            key: ("***" if key in _SECRET_KEYS and value[key] else redact(item))
            if key in _SECRET_KEYS
            else redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value


class NexentClient:
    """Thin, typed wrapper over the Nexent v2.8.0 HTTP surface."""

    def __init__(
        self,
        base_url: str = DEFAULT_CONFIG_URL,
        *,
        transport: Transport | None = None,
        stream_transport: StreamTransport | None = None,
        timeout: float = 60.0,
    ) -> None:
        self.base_url = _normalize_base(base_url)
        self._transport = transport or UrllibTransport(timeout)
        # ``UrllibTransport`` is callable for ordinary requests and exposes
        # ``stream`` for SSE; an injected callable replaces the latter only.
        self._stream_transport = stream_transport or UrllibTransport(timeout).stream
        self.jwt: str | None = None

    # ------------------------------------------------------------------
    # Transport
    # ------------------------------------------------------------------
    def _headers(
        self,
        extra: Mapping[str, str] | None = None,
        *,
        token: str | None = None,
    ) -> dict[str, str]:
        headers: dict[str, str] = {"Accept": "application/json"}
        auth = token or self.jwt
        if auth:
            headers["Authorization"] = f"Bearer {auth}"
        if extra:
            headers.update(extra)
        return headers

    def request(
        self,
        method: str,
        path: str,
        *,
        json_body: Any = None,
        body: bytes | None = None,
        content_type: str | None = None,
        headers: Mapping[str, str] | None = None,
        token: str | None = None,
        expected: Sequence[int] = (200,),
    ) -> Any:
        request_headers = self._headers(headers, token=token)
        if json_body is not None:
            body = json.dumps(json_body, ensure_ascii=False).encode("utf-8")
            content_type = "application/json"
        if content_type:
            request_headers["Content-Type"] = content_type
        url = f"{self.base_url}{path}"
        response = self._transport(method, url, request_headers, body)
        if response.status not in expected:
            raise NexentError(method, url, response.status, response.text())
        return response.json()

    def stream_events(
        self,
        method: str,
        path: str,
        *,
        json_body: Any = None,
        token: str | None = None,
    ) -> Iterator[dict[str, Any]]:
        body = (
            json.dumps(json_body, ensure_ascii=False).encode("utf-8")
            if json_body is not None
            else None
        )
        headers = self._headers(
            {"Content-Type": "application/json", "Accept": "text/event-stream"},
            token=token,
        )
        lines = self._stream_transport(method, f"{self.base_url}{path}", headers, body)
        yield from parse_sse_events(lines)

    # ------------------------------------------------------------------
    # Identity
    # ------------------------------------------------------------------
    def signin(self, email: str, password: str) -> dict[str, Any]:
        """Authenticate and remember the JWT for later calls."""
        payload = self.request(
            "POST",
            "/user/signin",
            json_body={"email": email, "password": password},
        )
        session = ((payload or {}).get("data") or {}).get("session") or {}
        token = session.get("access_token")
        if not token:
            raise NexentConfigError("signin response contains no access_token")
        self.jwt = token
        return payload

    def current_user(self) -> dict[str, Any]:
        payload = self.request("GET", "/user/current_user_info")
        user = ((payload or {}).get("data") or {}).get("user")
        if not isinstance(user, dict):
            raise NexentConfigError("current_user_info contains no user object")
        return user

    def ensure_api_key(self, user_id: str) -> str:
        """Return a usable northbound API key, creating one when necessary.

        Listing tokens returns the secret only for copy-capable roles
        (ADMIN/SU) and masks it otherwise; a masked or absent key falls back
        to creating a fresh token, which always returns the secret once and
        soft-deletes the caller's previous tokens.
        """
        payload = self.request(
            "GET",
            "/user/tokens?" + urllib.parse.urlencode({"user_id": user_id}),
        )
        for token in (payload or {}).get("data") or []:
            access_key = token.get("access_key")
            if (
                isinstance(access_key, str)
                and access_key
                and "*" not in access_key
                and token.get("can_copy", True)
            ):
                return access_key
        created = self.request("POST", "/user/tokens")
        access_key = ((created or {}).get("data") or {}).get("access_key")
        if not access_key:
            raise NexentConfigError("token creation response contains no access_key")
        return access_key

    # ------------------------------------------------------------------
    # Models
    # ------------------------------------------------------------------
    def list_models(self) -> list[dict[str, Any]]:
        payload = self.request("GET", "/model/list")
        return list((payload or {}).get("data") or [])

    def list_llm_models(self) -> list[dict[str, Any]]:
        payload = self.request("GET", "/model/llm_list")
        return list((payload or {}).get("data") or [])

    @staticmethod
    def _model_label(model: Mapping[str, Any]) -> str:
        return str(model.get("display_name") or model.get("model_name") or "")

    def resolve_embedding_model(self, preferred: str | None = None) -> dict[str, Any]:
        models = [
            model
            for model in self.list_models()
            if model.get("model_type") in {"embedding", "multi_embedding"}
        ]
        return _pick_model(models, preferred, "embedding")

    def resolve_llm_model(self, preferred: str | None = None) -> dict[str, Any]:
        return _pick_model(self.list_llm_models(), preferred, "LLM")

    # ------------------------------------------------------------------
    # MCP services
    # ------------------------------------------------------------------
    def list_mcp_servers(self) -> list[dict[str, Any]]:
        payload = self.request("GET", "/mcp/list")
        return list((payload or {}).get("remote_mcp_server_list") or [])

    def find_mcp(self, name: str) -> dict[str, Any] | None:
        for server in self.list_mcp_servers():
            if server.get("remote_mcp_server_name") == name:
                return server
        return None

    def test_mcp_connection(self, server_url: str) -> dict[str, Any]:
        return (
            self.request(
                "POST",
                "/mcp/test-connection",
                json_body={"server_url": server_url, "authorization_token": None},
            )
            or {}
        )

    def add_mcp(
        self,
        name: str,
        server_url: str,
        *,
        description: str = "",
        tags: Sequence[str] = (),
        enabled: bool = True,
    ) -> None:
        self.request(
            "POST",
            "/mcp/add",
            json_body={
                "name": name,
                "server_url": server_url,
                "description": description,
                "source": "local",
                "tags": list(tags),
                "enabled": enabled,
                "skip_health_check": False,
            },
        )

    def update_mcp(
        self,
        mcp_id: int,
        name: str,
        server_url: str,
        *,
        description: str = "",
        tags: Sequence[str] = (),
    ) -> None:
        self.request(
            "PUT",
            "/mcp/update",
            json_body={
                "mcp_id": mcp_id,
                "name": name,
                "server_url": server_url,
                "description": description,
                "tags": list(tags),
            },
        )

    def enable_mcp(self, mcp_id: int) -> None:
        self.request("POST", "/mcp/enable", json_body={"mcp_id": mcp_id})

    def healthcheck_mcp(self, mcp_id: int) -> Any:
        payload = self.request(
            "GET", "/mcp/healthcheck?" + urllib.parse.urlencode({"mcp_id": mcp_id})
        )
        return ((payload or {}).get("data") or {}).get("health_status")

    # ------------------------------------------------------------------
    # Tools
    # ------------------------------------------------------------------
    def scan_tools(self) -> None:
        self.request("GET", "/tool/scan_tool")

    def list_tools(self) -> list[dict[str, Any]]:
        payload = self.request("GET", "/tool/list")
        if not isinstance(payload, list):
            raise NexentConfigError("/tool/list did not return a JSON array")
        return payload

    # ------------------------------------------------------------------
    # Skills
    # ------------------------------------------------------------------
    def list_skills(self) -> list[dict[str, Any]]:
        payload = self.request("GET", "/skills")
        if isinstance(payload, list):
            return payload
        return list((payload or {}).get("skills") or [])

    def delete_skill(self, name: str) -> None:
        self.request(
            "DELETE",
            "/skills/" + urllib.parse.quote(name, safe=""),
            expected=(200, 400, 404),
        )

    # ------------------------------------------------------------------
    # Knowledge base
    # ------------------------------------------------------------------
    def check_index(self, knowledge_name: str) -> str:
        payload = self.request(
            "POST",
            "/indices/check_exist",
            json_body={"knowledge_name": knowledge_name},
        )
        return str((payload or {}).get("status") or "")

    def create_index(
        self, knowledge_name: str, embedding_model_id: int
    ) -> dict[str, Any]:
        payload = self.request(
            "POST",
            "/indices/" + urllib.parse.quote(knowledge_name, safe=""),
            json_body={"embedding_model_id": embedding_model_id},
        )
        if not isinstance(payload, dict) or not payload.get("id"):
            raise NexentConfigError(
                f"index creation for {knowledge_name!r} returned no internal id"
            )
        return payload

    def list_indices(self) -> list[dict[str, Any]]:
        payload = self.request(
            "GET", "/indices?" + urllib.parse.urlencode({"include_stats": "true"})
        )
        return list((payload or {}).get("indices_info") or [])

    def find_index_by_display_name(self, display_name: str) -> dict[str, Any] | None:
        # ``display_name`` is the user-facing name; older builds only return
        # the internal ``name``, so match either.
        for info in self.list_indices():
            if display_name in {info.get("display_name"), info.get("name")}:
                return info
        return None

    def list_index_files(self, index_name: str) -> list[dict[str, Any]]:
        payload = self.request(
            "GET",
            "/indices/" + urllib.parse.quote(index_name, safe="") + "/files",
        )
        return list((payload or {}).get("files") or [])

    def upload_files(
        self,
        index_name: str,
        paths: Sequence[Path],
        *,
        destination: str = "minio",
        folder: str = "knowledge_base",
    ) -> dict[str, Any]:
        fields = [
            ("index_name", index_name),
            ("destination", destination),
            ("folder", folder),
        ]
        files = [
            (
                "file",
                path.name,
                path.read_bytes(),
                _guess_content_type(path),
            )
            for path in paths
        ]
        body, content_type = encode_multipart(fields, files)
        return (
            self.request(
                "POST",
                "/file/upload",
                body=body,
                content_type=content_type,
            )
            or {}
        )

    def process_files(
        self,
        index_name: str,
        files: Sequence[Mapping[str, Any]],
        *,
        destination: str = "minio",
        chunking_strategy: str = "basic",
    ) -> Any:
        return self.request(
            "POST",
            "/file/process",
            json_body={
                "files": list(files),
                "index_name": index_name,
                "destination": destination,
                "chunking_strategy": chunking_strategy,
            },
            # The route declares 201 Created: the body is the task submission
            # result, not an error.
            expected=(200, 201),
        )

    # ------------------------------------------------------------------
    # Agents
    # ------------------------------------------------------------------
    def get_agent_by_name(self, name: str) -> dict[str, Any] | None:
        try:
            payload = self.request(
                "GET",
                "/agent/by-name/" + urllib.parse.quote(name, safe=""),
            )
        except NexentError as exc:
            if exc.status == 404:
                return None
            raise
        return payload if isinstance(payload, dict) else None

    def delete_agent(self, agent_id: int) -> None:
        self.request(
            "DELETE", "/agent", json_body={"agent_id": agent_id}, expected=(200, 404)
        )

    def get_agent_call_relationship(self, agent_id: int) -> dict[str, Any]:
        """Return the persisted manager-to-sub-agent call tree."""

        payload = self.request("GET", f"/agent/call_relationship/{agent_id}")
        if not isinstance(payload, dict):
            raise NexentConfigError("/agent/call_relationship returned no object")
        return payload

    def import_agent(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        result = self.request("POST", "/agent/import", json_body=payload)
        if not isinstance(result, dict) or "agent_id" not in result:
            raise NexentConfigError("/agent/import returned no agent_id")
        return result


def _pick_model(
    models: Sequence[Mapping[str, Any]], preferred: str | None, label: str
) -> dict[str, Any]:
    if not models:
        raise NexentConfigError(f"Nexent has no {label} model configured")
    if not preferred:
        return dict(models[0])
    for model in models:
        if preferred in {
            str(model.get("display_name") or ""),
            str(model.get("model_name") or ""),
        }:
            return dict(model)
    available = ", ".join(NexentClient._model_label(model) for model in models)
    raise NexentConfigError(
        f"no {label} model matches {preferred!r}; available: {available}"
    )


def _guess_content_type(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in {".md", ".markdown"}:
        return "text/markdown"
    if suffix == ".txt":
        return "text/plain"
    if suffix == ".json":
        return "application/json"
    if suffix == ".pdf":
        return "application/pdf"
    return "application/octet-stream"
