FROM nexent/nexent:v2.8.0

COPY nexent_dsml_compat.py /tmp/nexent_dsml_compat.py
COPY nexent_dsml_compat.pth /tmp/nexent_dsml_compat.pth

RUN python - <<'PY'
import shutil
import site
from pathlib import Path

target = Path(site.getsitepackages()[0])
shutil.copy2("/tmp/nexent_dsml_compat.py", target / "nexent_dsml_compat.py")
shutil.copy2("/tmp/nexent_dsml_compat.pth", target / "nexent_dsml_compat.pth")
PY
