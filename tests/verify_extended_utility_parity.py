from __future__ import annotations

import json
import os
import subprocess
import sys
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tests" / "_extended_utility_snapshot.py"


def snapshot(*, shim: bool) -> dict[str, object]:
    result = subprocess.run(
        [sys.executable, str(SCRIPT)],
        cwd=ROOT,
        env={**os.environ, "SPIDEROXIDE_SHIM": "1" if shim else "0"},
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(result.stdout)


if __name__ == "__main__":
    candidate = snapshot(shim=True)
    try:
        version("Scrapy")
    except PackageNotFoundError:
        assert {"python", "misc", "project", "response", "deprecate"} <= candidate.keys()
    else:
        upstream = snapshot(shim=False)
        for key, expected in upstream.items():
            assert candidate[key] == expected, (key, expected, candidate.get(key))
    print("Extended Scrapy utilities passed: Python, misc, deprecation, project and response")
