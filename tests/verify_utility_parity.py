from __future__ import annotations

import json
import os
import subprocess
import sys
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tests" / "_utility_snapshot.py"


def snapshot(*, shim: bool) -> dict[str, object]:
    env = {**os.environ, "SPIDEROXIDE_SHIM": "1" if shim else "0"}
    result = subprocess.run(
        [sys.executable, str(SCRIPT)],
        cwd=ROOT,
        env=env,
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
        assert {
            "crawled",
            "curl",
            "logging",
            "mail",
            "url",
            "serialized",
            "job_dir",
        } <= candidate.keys()
    else:
        upstream = snapshot(shim=False)
        for key, value in upstream.items():
            assert candidate[key] == value, (key, value, candidate[key])
    print("Utility parity passed: formatter records, exceptions, curl, URL, JSON and mail APIs")
