"""Tests for the new-source W9 downloader."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.fetch_w9_sources import (
    SourceError,
    VisibleTextParser,
    download_sources,
)


def test_parser_preserves_visible_table_text() -> None:
    parser = VisibleTextParser()
    parser.feed(
        """
        <html><head><title>泵故障</title><style>bad</style></head>
        <body><h1>离心泵</h1><script>ignored()</script>
        <table><tr><th>故障</th><th>原因</th></tr>
        <tr><td>不出水</td><td>吸入管路漏气</td></tr></table></body></html>
        """
    )

    text = parser.visible_text()
    assert parser.title() == "泵故障"
    assert "不出水 | 吸入管路漏气" in text
    assert "ignored" not in text
    assert "bad" not in text


def test_downloader_writes_manifest_and_detects_content_changes(
    tmp_path: Path,
) -> None:
    registry = tmp_path / "sources.json"
    registry.write_text(
        json.dumps(
            {
                "version": 1,
                "usage_policy": "test",
                "sources": [
                    {
                        "id": "pump-source",
                        "title": "泵故障",
                        "publisher": "测试厂商",
                        "url": "https://example.test/pump",
                        "required_keywords": ["离心泵", "故障"],
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    body = (
        "<html><title>泵故障</title><body><h1>离心泵</h1>"
        + "<p>离心泵常见故障及排除方法。</p>" * 30
        + "</body></html>"
    ).encode()
    calls = {"count": 0}

    def fetcher(url: str) -> tuple[bytes, str]:
        calls["count"] += 1
        return body, url

    output = tmp_path / "corpus"
    first, items = download_sources(
        registry_path=registry,
        output_dir=output,
        fetcher=fetcher,
    )
    second, items_again = download_sources(
        registry_path=registry,
        output_dir=output,
        fetcher=fetcher,
    )

    assert calls["count"] == 2
    assert items[0].text_characters >= 300
    assert items[0].content_changed is False
    assert items_again[0].content_changed is False
    assert first["sources"][0]["raw_sha256"] == second["sources"][0]["raw_sha256"]
    assert (output / "manifest.json").is_file()
    assert (output / "raw" / "pump-source.html").is_file()
    assert (output / "parsed" / "pump-source.txt").is_file()


def test_changed_live_content_requires_explicit_refreeze(tmp_path: Path) -> None:
    registry = tmp_path / "sources.json"
    registry.write_text(
        json.dumps(
            {
                "sources": [
                    {
                        "id": "pump-source",
                        "title": "泵故障",
                        "publisher": "测试厂商",
                        "url": "https://example.test/pump",
                        "required_keywords": ["离心泵", "故障"],
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    initial = (
        "<html><body>离心泵故障。"
        + "<p>离心泵常见故障及排除方法。</p>" * 40
        + "</body></html>"
    ).encode()
    changed = (
        "<html><body>离心泵故障更新。"
        + "<p>离心泵常见故障及排除方法已更新。</p>" * 40
        + "</body></html>"
    ).encode()
    output = tmp_path / "corpus"
    download_sources(
        registry_path=registry,
        output_dir=output,
        fetcher=lambda url: (initial, url),
    )

    with pytest.raises(SourceError, match="content changed"):
        download_sources(
            registry_path=registry,
            output_dir=output,
            fetcher=lambda url: (changed, url),
        )

    _, items = download_sources(
        registry_path=registry,
        output_dir=output,
        fetcher=lambda url: (changed, url),
        allow_content_change=True,
    )
    assert items[0].content_changed is True
