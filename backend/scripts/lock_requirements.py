"""Write requirements.lock: the exact installed version of every package in requirements.txt."""
import re
from importlib import metadata
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
out = ["# Exact versions this release was tested with: pip install -r requirements.lock",
       "# Regenerate after upgrading: python scripts/lock_requirements.py"]
for raw in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines():
    raw = raw.strip()
    if not raw or raw.startswith("#"):
        continue
    name = re.split(r"[<>=\[ ]", raw, 1)[0]
    extras = re.search(r"\[.*?\]", raw)
    out.append(f"{name}{extras.group(0) if extras else ''}=={metadata.version(name)}")
(ROOT / "requirements.lock").write_text("\n".join(out) + "\n", encoding="utf-8")
print("\n".join(out))
