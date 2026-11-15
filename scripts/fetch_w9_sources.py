#!/usr/bin/env python
"""Download and freeze the new, W7-unseen Chinese W9 web sources."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from typing import Callable, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCES = ROOT / "eval" / "w9_sources.json"
DEFAULT_OUTPUT = ROOT / "eval" / "corpus"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 Chrome/120 Safari/537.36"
)


class SourceError(RuntimeError):
    """A source cannot be downloaded or trusted as evaluation data."""


@dataclass(frozen=True)
class DownloadedSource:
    source_id: str
    title: str
    publisher: str
    url: str
    final_url: str
    fetched_at: str
    raw_sha256: str
    text_sha256: str
    raw_bytes: int
    text_characters: int
    text_path: str
    raw_path: str
    content_changed: bool


class VisibleTextParser(HTMLParser):
    """Extract visible text while preserving table rows and block boundaries."""

    _SKIP = {"script", "style", "noscript", "svg"}
    _BLOCK = {
        "br",
        "div",
        "p",
        "li",
        "tr",
        "section",
        "article",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self._parts: list[str] = []
        self.title_parts: list[str] = []
        self._in_title = False

    def handle_starttag(self, tag: str, attrs) -> None:
        tag = tag.casefold()
        if tag in self._SKIP:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        if tag == "title":
            self._in_title = True
        elif tag in self._BLOCK:
            self._parts.append("\n")
        elif tag in {"td", "th"}:
            self._parts.append(" | ")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.casefold()
        if tag in self._SKIP and self._skip_depth:
            self._skip_depth -= 1
            return
        if self._skip_depth:
            return
        if tag == "title":
            self._in_title = False
        elif tag in self._BLOCK:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        if self._in_title:
            self.title_parts.append(data)
            return
        if data.strip():
            self._parts.append(data)

    def visible_text(self) -> str:
        text = unescape("".join(self._parts)).replace("\xa0", " ")
        lines = []
        for line in text.splitlines():
            compact = re.sub(r"[ \t]+", " ", line).strip(" |")
            if compact:
                lines.append(compact)
        return "\n".join(lines).strip() + "\n"

    def title(self) -> str:
        return re.sub(r"\s+", " ", "".join(self.title_parts)).strip()


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _read_json(path: Path) -> dict:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise SourceError(f"{path} must contain a JSON object")
    return raw


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def _http_fetch(url: str, timeout: float) -> tuple[bytes, str]:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": "text/html,*/*;q=0.8"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            content_type = str(response.headers.get("Content-Type") or "")
            if "html" not in content_type.casefold():
                raise SourceError(
                    f"{url} returned unsupported content type {content_type!r}"
                )
            body = response.read()
            return body, response.geturl()
    except urllib.error.HTTPError as exc:
        raise SourceError(f"{url} returned HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise SourceError(f"cannot download {url}: {exc}") from exc


def download_sources(
    *,
    registry_path: Path = DEFAULT_SOURCES,
    output_dir: Path = DEFAULT_OUTPUT,
    fetcher: Callable[[str], tuple[bytes, str]] | None = None,
) -> tuple[dict, list[DownloadedSource]]:
    registry = _read_json(registry_path)
    raw_items = registry.get("sources")
    if not isinstance(raw_items, list) or not raw_items:
        raise SourceError(f"{registry_path} has no sources")
    fetch = fetcher or (lambda url: _http_fetch(url, 60.0))
    raw_dir = output_dir / "raw"
    parsed_dir = output_dir / "parsed"
    previous = _load_previous_manifest(output_dir / "manifest.json")
    previous_by_id = {item["source_id"]: item for item in previous.get("sources", [])}
    downloaded: list[DownloadedSource] = []
    seen: set[str] = set()
    for source in raw_items:
        if not isinstance(source, dict):
            raise SourceError("every source entry must be an object")
        source_id = _safe_token(source.get("id"), "source.id")
        if source_id in seen:
            raise SourceError(f"duplicate source id {source_id!r}")
        seen.add(source_id)
        title = _required(source, "title")
        publisher = _required(source, "publisher")
        url = _required(source, "url")
        raw_bytes, final_url = fetch(url)
        parser = VisibleTextParser()
        try:
            parser.feed(raw_bytes.decode("utf-8"))
        except UnicodeDecodeError:
            parser.feed(raw_bytes.decode("gb18030", errors="replace"))
        text = parser.visible_text()
        _validate_text(source_id, source, text)
        raw_path = raw_dir / f"{source_id}.html"
        text_path = parsed_dir / f"{source_id}.txt"
        raw_path.parent.mkdir(parents=True, exist_ok=True)
        raw_path.write_bytes(raw_bytes)
        _write_text(text_path, text)
        raw_digest = _sha256(raw_bytes)
        previous_item = previous_by_id.get(source_id) or {}
        item = DownloadedSource(
            source_id=source_id,
            title=title,
            publisher=publisher,
            url=url,
            final_url=final_url,
            fetched_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            raw_sha256=raw_digest,
            text_sha256=_sha256(text.encode("utf-8")),
            raw_bytes=len(raw_bytes),
            text_characters=len(text),
            text_path=_display_path(text_path),
            raw_path=_display_path(raw_path),
            content_changed=bool(
                previous_item
                and previous_item.get("text_sha256") != _sha256(text.encode("utf-8"))
            ),
        )
        downloaded.append(item)
    manifest = {
        "generated_by": "scripts/fetch_w9_sources.py",
        "registry": _display_path(registry_path),
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "usage_policy": registry.get("usage_policy"),
        "sources": [item.__dict__ for item in downloaded],
    }
    _write_text(
        output_dir / "manifest.json",
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )
    return manifest, downloaded


def _validate_text(source_id: str, source: Mapping, text: str) -> None:
    chinese = len(re.findall(r"[\u3400-\u9fff]", text))
    if chinese < 300:
        raise SourceError(f"{source_id} extracted only {chinese} Chinese characters")
    missing = [
        keyword
        for keyword in source.get("required_keywords") or []
        if str(keyword) not in text
    ]
    if missing:
        raise SourceError(f"{source_id} is missing keywords: {missing}")


def _load_previous_manifest(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _required(source: Mapping, key: str) -> str:
    value = source.get(key)
    if not isinstance(value, str) or not value.strip():
        raise SourceError(f"source.{key} must be a non-empty string")
    return value.strip()


def _safe_token(value: object, name: str) -> str:
    text = str(value or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9._-]+", text):
        raise SourceError(f"{name} must be filename-safe")
    return text


def _display_path(path: Path) -> str:
    resolved = path.resolve()
    try:
        return str(resolved.relative_to(ROOT)).replace("\\", "/")
    except ValueError:
        return str(resolved)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sources", type=Path, default=DEFAULT_SOURCES)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUTPUT)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        manifest, downloaded = download_sources(
            registry_path=args.sources.resolve(),
            output_dir=args.out_dir.resolve(),
        )
    except SourceError as exc:
        print(f"[error] {exc}", file=sys.stderr)
        return 1
    for item in downloaded:
        changed = " CHANGED" if item.content_changed else ""
        print(
            f"{item.source_id}: {item.text_characters} chars "
            f"sha256:{item.text_sha256[:12]}{changed}"
        )
    print(
        json.dumps(
            {
                "ok": True,
                "sources": len(manifest["sources"]),
                "manifest": str((args.out_dir / "manifest.json").resolve()),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "DownloadedSource",
    "SourceError",
    "VisibleTextParser",
    "build_parser",
    "download_sources",
    "main",
]
