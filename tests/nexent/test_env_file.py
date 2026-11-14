"""Tests for the optional Nexent environment-file loader."""

from __future__ import annotations

from pathlib import Path

from nexent.scripts.env_file import load_nexent_env


def test_load_nexent_env_reads_shell_style_values_without_overwriting(
    tmp_path: Path,
) -> None:
    source = tmp_path / ".env"
    source.write_text(
        "\n".join(
            [
                "# comment",
                "NEXENT_CONFIG_URL=http://localhost:5010",
                "export NEXENT_EMAIL='operator@example.com'",
                'NEXENT_PASSWORD="secret with spaces"',
                "INVALID_LINE",
            ]
        ),
        encoding="utf-8",
    )
    environ = {"NEXENT_PASSWORD": "already-set"}

    loaded = load_nexent_env(source, environ=environ)

    assert environ == {
        "NEXENT_PASSWORD": "already-set",
        "NEXENT_CONFIG_URL": "http://localhost:5010",
        "NEXENT_EMAIL": "operator@example.com",
    }
    assert loaded == ["NEXENT_CONFIG_URL", "NEXENT_EMAIL"]


def test_load_nexent_env_ignores_a_missing_file(tmp_path: Path) -> None:
    environ: dict[str, str] = {}

    assert load_nexent_env(tmp_path / ".env", environ=environ) == []
    assert environ == {}
