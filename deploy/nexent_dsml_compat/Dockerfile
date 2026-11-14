FROM nexent/nexent:v2.8.0

COPY nexent_dsml_compat.py /tmp/nexent_dsml_compat.py

RUN python - <<'PY'
import shutil
import site
from pathlib import Path

target = Path(site.getsitepackages()[0])
shutil.copy2("/tmp/nexent_dsml_compat.py", target / "nexent_dsml_compat.py")

module_path = target / "nexent" / "core" / "agents" / "output_protocol.py"
if not module_path.is_file():
    raise RuntimeError(f"Nexent output_protocol module not found: {module_path}")
source = module_path.read_text(encoding="utf-8")
import_anchor = (
    "from .clarification import "
    "CLARIFICATION_SCHEMA_GUIDANCE, ClarificationForm\n"
)
call_anchor = (
    '    if protocol not in ("code_action", "final_envelope"):\n'
    '        raise ValueError(f"Unsupported output protocol: {protocol}")\n'
)
if "from nexent_dsml_compat import normalize_dsml_tool_calls" not in source:
    if import_anchor not in source:
        raise RuntimeError("Nexent output_protocol import anchor not found")
    source = source.replace(
        import_anchor,
        import_anchor
        + "from nexent_dsml_compat import normalize_dsml_tool_calls\n",
        1,
    )
if "output = normalize_dsml_tool_calls(output)" not in source:
    if call_anchor not in source:
        raise RuntimeError("Nexent classify_model_output anchor not found")
    source = source.replace(
        call_anchor,
        call_anchor + "    output = normalize_dsml_tool_calls(output)\n",
        1,
    )
module_path.write_text(source, encoding="utf-8")
print(f"patched {module_path}")
PY
