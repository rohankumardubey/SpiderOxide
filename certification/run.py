from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import sys
from importlib.metadata import version
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = Path(__file__).with_name("manifest.json")
PROBE = Path(__file__).with_name("probe.py")
REQUIREMENTS = ROOT / "requirements-certification.txt"


def _versions() -> dict[str, str]:
    pinned = {}
    for line in REQUIREMENTS.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name, separator, expected = line.partition("==")
        if not separator or not name or not expected:
            raise ValueError(f"certification dependency must be exactly pinned: {line}")
        actual = version(name)
        if actual != expected:
            raise RuntimeError(f"certification requires {name}=={expected}; found {actual}")
        pinned[name] = actual
    return pinned


def _run(backend: str, case: str, expected: str) -> dict[str, str]:
    environment = dict(os.environ)
    environment["PYTHONPATH"] = os.pathsep.join(
        (str(ROOT / "compat" / "scrapy" / "src"), str(ROOT / "src"))
    )
    environment["SPIDEROXIDE_SCRAPY_COMPAT_ALLOW_CONFLICT"] = "1"
    result = subprocess.run(
        [sys.executable, str(PROBE), backend, case],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=90,
    )
    return {
        "status": (expected if expected == "blocked" else "pass")
        if result.returncode == 0
        else "fail",
        "output": (result.stdout + result.stderr).strip(),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Certify pinned third-party Scrapy integrations")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "certification-results")
    args = parser.parse_args()
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    versions = _versions()
    case_ids = [entry["id"] for entry in manifest["integrations"]]
    if len(set(case_ids)) != len(case_ids):
        raise ValueError("duplicate third-party certification case")
    for entry in manifest["integrations"]:
        if entry["distribution"] not in versions:
            raise ValueError(f"unpinned certification package: {entry['distribution']}")
        if entry["status"] not in {"certified", "blocked"}:
            raise ValueError(f"invalid certification status for {entry['id']}")

    results = {
        backend: {
            entry["id"]: _run(backend, entry["id"], entry["status"])
            for entry in manifest["integrations"]
        }
        for backend in ("python", "rust")
    }
    report = {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "spideroxide": version("spideroxide"),
        "versions": versions,
        "integrations": manifest["integrations"],
        "results": results,
    }
    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "certification.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    lines = [
        "# Third-party Scrapy certification",
        "",
        f"Python: `{report['python']}`; platform: `{report['platform']}`; "
        f"SpiderOxide: `{report['spideroxide']}`.",
        "",
        "| Integration | Pinned version | Scope | Python | Rust |",
        "|---|---|---|---|---|",
    ]
    failed = False
    for entry in manifest["integrations"]:
        case = entry["id"]
        statuses = [results[backend][case]["status"] for backend in ("python", "rust")]
        failed |= "fail" in statuses
        lines.append(
            f"| {case} | {entry['distribution']}=={versions[entry['distribution']]} | "
            f"{entry['scope']} | {statuses[0]} | {statuses[1]} |"
        )
    lines.extend(
        [
            "",
            "A blocked result verifies the documented failure; it does not certify compatibility.",
            "",
            "| Integration | Required API | Limitation or blocker |",
            "|---|---|---|",
        ]
    )
    for entry in manifest["integrations"]:
        lines.append(
            f"| {entry['id']} | {', '.join(entry['public_apis'])} | {entry['limitations']} |"
        )
    lines.append("")
    (args.output_dir / "certification.md").write_text("\n".join(lines), encoding="utf-8")
    for backend, cases in results.items():
        for case, result in cases.items():
            print(f"{backend}/{case}: {result['status']}", flush=True)
            if result["status"] == "fail":
                print(result["output"], file=sys.stderr)
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
