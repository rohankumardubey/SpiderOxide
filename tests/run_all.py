from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    verification_scripts = sorted(Path(__file__).parent.glob("verify_*.py"))
    for script in verification_scripts:
        print(f"Running {script.relative_to(ROOT)}", flush=True)
        subprocess.run([sys.executable, str(script)], cwd=ROOT, check=True)


if __name__ == "__main__":
    main()
