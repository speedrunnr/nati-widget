"""After each nati_ff.py run: bring the v2 widget's data up to date and rebuild the artifact bundle.

  1. copy the index build (site/data: index, symbols, constituents, reviews, series) into v2/site/data
  2. company profiles: quotes and recent bars from nati_ff.py's download (build_profiles.py --prices-only)
  3. Key Insights (build_insights.py)
  4. the claude.ai artifact bundle (make_artifact.py), when that script is present

It reads nati_ff.py's output and cache and never writes to them. No network calls, except a full
profile build for a company that has newly joined the index.

    .venv/bin/python v2/refresh_v2.py
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

V2 = Path(__file__).resolve().parent
ROOT = V2.parent
SRC, DST = ROOT / "site" / "data", V2 / "site" / "data"
PY = sys.executable


def copy(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_suffix(dst.suffix + ".tmp")
    shutil.copyfile(src, tmp)
    os.replace(tmp, dst)


def run(*args: str) -> None:
    print(f"$ {' '.join(args)}", flush=True)
    subprocess.run([PY, *args], cwd=ROOT, check=True)


def main() -> int:
    n = 0
    for name in ("symbols.json", "constituents.json", "reviews.json"):
        copy(SRC / name, DST / name); n += 1
    for p in sorted((SRC / "series").glob("*.json")):
        copy(p, DST / "series" / p.name); n += 1
    copy(SRC / "index.json", DST / "index.json"); n += 1          # last: pages poll index.json for a new build
    print(f"copied {n} index files from site/data", flush=True)
    run("v2/build_profiles.py", "--prices-only")
    run("v2/build_insights.py")
    if (V2 / "make_artifact.py").exists():                       # the claude.ai artifact build, where it is used
        run("v2/make_artifact.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
