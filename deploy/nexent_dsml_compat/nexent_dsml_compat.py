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
import builtins
import html
import json
import re
import sys
from typing import Any

_BAR = r"[|｜]"
_OPEN = rf"<\s*{_BAR}\s*DSML\s*{_BAR}"
_CLOSE = rf"</\s*{_BAR}\s*DSML\s*{_BAR}"
_INVOKE_RE = re.compile(
    rf"{_OPEN}\s*invoke\s+name=\"(?P<name>[A-Za-z_]\w*)\"\s*>"
    rf"(?P<body>[\s\S]*?)"
    rf"{_CLOSE}\s*invoke\s*>",
    re.IGNORECASE,
)
_PARAMETER_RE = re.compile(
    rf"{_OPEN}\s*parameter\s+name=\"(?P<name>[A-Za-z_]\w*)\""
    rf"(?:\s+string=\"(?P<string>true|false)\")?\s*>"
    rf"(?P<value>[\s\S]*?)"
    rf"{_CLOSE}\s*parameter\s*>",
    re.IGNORECASE,
)
_DSML_MARKER_RE = re.compile(_OPEN, re.IGNORECASE)
_HOOK_INSTALLED = False
_PATCHED = False
_ORIGINAL_IMPORT = builtins.__import__


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
    """Install the patch now or when Nexent's protocol module is imported."""

    global _HOOK_INSTALLED
    if _PATCHED or _HOOK_INSTALLED:
        return True
    try:
        from nexent.core.agents import output_protocol  # noqa: F401
    except ImportError:
        _install_import_hook()
        return True
    _patch_loaded_modules()
    return _PATCHED


def _install_import_hook() -> None:
    global _HOOK_INSTALLED
    if _HOOK_INSTALLED:
        return

    def import_with_dsml_compat(
        name: str,
        globals: dict[str, Any] | None = None,
        locals: dict[str, Any] | None = None,
        fromlist: tuple[str, ...] = (),
        level: int = 0,
    ) -> Any:
        module = _ORIGINAL_IMPORT(name, globals, locals, fromlist, level)
        if not _PATCHED:
            _patch_loaded_modules()
        return module

    builtins.__import__ = import_with_dsml_compat
    _HOOK_INSTALLED = True


def _patch_loaded_modules() -> bool:
    global _HOOK_INSTALLED, _PATCHED
    output_protocol = sys.modules.get("nexent.core.agents.output_protocol")
    if output_protocol is None:
        return False

    original = output_protocol.classify_model_output
    if getattr(original, "_nexent_dsml_compat", False):
        _PATCHED = True
        return True

    def classify_with_dsml(output: Any, **kwargs: Any) -> Any:
        return original(normalize_dsml_tool_calls(output), **kwargs)

    classify_with_dsml._nexent_dsml_compat = True  # type: ignore[attr-defined]
    classify_with_dsml.__name__ = getattr(original, "__name__", "classify_model_output")
    classify_with_dsml.__doc__ = getattr(original, "__doc__")
    output_protocol.classify_model_output = classify_with_dsml

    # ``core_agent`` imports the function by name, so patch that binding too if
    # the module was already loaded by the time this hook runs.
    core_agent = sys.modules.get("nexent.core.agents.core_agent")
    if core_agent is not None and hasattr(core_agent, "classify_model_output"):
        core_agent.classify_model_output = classify_with_dsml

    if _HOOK_INSTALLED:
        builtins.__import__ = _ORIGINAL_IMPORT
        _HOOK_INSTALLED = False
    _PATCHED = True
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
