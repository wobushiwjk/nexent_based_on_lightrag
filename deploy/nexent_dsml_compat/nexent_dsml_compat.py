"""Translate DeepSeek DSML tool calls into Nexent CodeAgent actions.

Nexent 2.8.0 accepts Python actions inside ``<code>`` blocks. A DeepSeek
OpenAI-compatible endpoint may instead return a textual DSML tool-call block in
the assistant content. This module installs a narrow compatibility wrapper at
the output-protocol boundary, before Nexent classifies the model output.

The wrapper is intentionally conservative: it accepts only identifier-shaped
tool names, decodes JSON-like arguments without ``eval``, and leaves unknown
formats unchanged so Nexent can report its normal protocol error.
"""

from __future__ import annotations

import ast
import html
import json
import re
from typing import Any

_BAR = r"[|｜]"
_INVOKE_RE = re.compile(
    rf"<{_BAR}\s*DSML\s*{_BAR}\s*invoke\s+name=\"(?P<name>[A-Za-z_]\w*)\"\s*>"
    rf"(?P<body>[\s\S]*?)"
    rf"</{_BAR}\s*DSML\s*{_BAR}\s*invoke\s*>",
    re.IGNORECASE,
)
_PARAMETER_RE = re.compile(
    rf"<{_BAR}\s*DSML\s*{_BAR}\s*parameter\s+name=\"(?P<name>[A-Za-z_]\w*)\""
    rf"(?:\s+string=\"(?P<string>true|false)\")?\s*>"
    rf"(?P<value>[\s\S]*?)"
    rf"</{_BAR}\s*DSML\s*{_BAR}\s*parameter\s*>",
    re.IGNORECASE,
)
_DSML_MARKER_RE = re.compile(rf"<{_BAR}\s*DSML\s*{_BAR}", re.IGNORECASE)
_INSTALLED = False


def normalize_dsml_tool_calls(output: Any) -> Any:
    """Return a Nexent ``<code>`` action when ``output`` contains DSML calls."""

    if not isinstance(output, str) or not _DSML_MARKER_RE.search(output):
        return output

    calls: list[str] = []
    for invoke in _INVOKE_RE.finditer(output):
        tool_name = invoke.group("name")
        arguments = [
            f"{parameter.group('name')}={_decode_parameter(parameter)}"
            for parameter in _PARAMETER_RE.finditer(invoke.group("body"))
        ]
        calls.append(f"{tool_name}({', '.join(arguments)})")
    if not calls:
        return output
    return "<code>\n" + "\n".join(calls) + "\n</code>"


def install() -> bool:
    """Patch ``classify_model_output`` once and return whether it is active."""

    global _INSTALLED
    if _INSTALLED:
        return True
    try:
        from nexent.core.agents import output_protocol
    except ImportError:
        return False

    original = output_protocol.classify_model_output
    if getattr(original, "_nexent_dsml_compat", False):
        _INSTALLED = True
        return True

    def classify_with_dsml(output: Any, **kwargs: Any) -> Any:
        return original(normalize_dsml_tool_calls(output), **kwargs)

    classify_with_dsml._nexent_dsml_compat = True  # type: ignore[attr-defined]
    classify_with_dsml.__name__ = getattr(original, "__name__", "classify_model_output")
    classify_with_dsml.__doc__ = getattr(original, "__doc__")
    output_protocol.classify_model_output = classify_with_dsml
    _INSTALLED = True
    return True


def _decode_parameter(match: re.Match[str]) -> str:
    name = match.group("name")
    marker = match.group("string")
    raw_value = html.unescape(match.group("value"))
    if marker == "true":
        value: Any = raw_value
    else:
        value = _decode_json_value(raw_value, parameter=name)
    return repr(value)


def _decode_json_value(raw_value: str, *, parameter: str) -> Any:
    value = raw_value.strip()
    if not value:
        return ""
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        pass
    try:
        parsed = ast.literal_eval(value)
    except (SyntaxError, ValueError):
        return raw_value
    if isinstance(parsed, (str, int, float, bool, list, dict, tuple)) or parsed is None:
        return parsed
    raise ValueError(f"unsupported DSML parameter value for {parameter!r}")


install()


__all__ = ["install", "normalize_dsml_tool_calls"]
