from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
PROBE = Path(__file__).with_name("probe.py")
MANIFEST = Path(__file__).with_name("manifest.json")


def _run_probe(implementation: str, backend: str) -> dict[str, Any]:
    environment = dict(os.environ)
    if implementation == "spideroxide":
        environment["PYTHONPATH"] = os.pathsep.join(
            (
                str(ROOT / "compat" / "scrapy" / "src"),
                str(ROOT / "src"),
            )
        )
        environment["SPIDEROXIDE_SCRAPY_COMPAT_ALLOW_CONFLICT"] = "1"
    else:
        environment.pop("PYTHONPATH", None)
        environment.pop("SPIDEROXIDE_SCRAPY_COMPAT_ALLOW_CONFLICT", None)
    result = subprocess.run(
        [
            sys.executable,
            str(PROBE),
            "--implementation",
            implementation,
            "--backend",
            backend,
        ],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        raise RuntimeError(
            f"{implementation}/{backend} probe failed:\n{result.stdout}{result.stderr}"
        )
    return json.loads(result.stdout)


def _case_sources(manifest: dict[str, Any]) -> dict[str, list[str]]:
    return {
        case["id"]: case["upstream_tests"]
        for case in manifest["cases"]
        if case["status"] == "adapted"
    }


def _compare(
    baseline: dict[str, Any],
    candidate: dict[str, Any],
    sources: dict[str, list[str]],
) -> list[dict[str, object]]:
    results = []
    for case_id, upstream_tests in sources.items():
        expected = baseline["cases"][case_id]
        actual = candidate["cases"][case_id]
        results.append(
            {
                "id": case_id,
                "status": "pass" if actual == expected else "fail",
                "upstream_tests": upstream_tests,
                "expected": expected if actual != expected else None,
                "actual": actual if actual != expected else None,
            }
        )
    return results


def _markdown(report: dict[str, Any], manifest: dict[str, Any]) -> str:
    baseline = report["baseline"]
    lines = [
        "# Scrapy conformance report",
        "",
        f"- Generated: `{report['generated_at']}`",
        f"- Scrapy: `{baseline['environment']['scrapy']}`",
        f"- SpiderOxide: `{report['candidates'][0]['environment']['spideroxide']}`",
        f"- Python: `{baseline['environment']['python']}`",
        f"- Platform: `{baseline['environment']['platform']}`",
        "",
        "## Differential results",
        "",
        "| Case | Python backend | Rust backend | Upstream tests |",
        "|---|---:|---:|---|",
    ]
    by_backend = {
        candidate["backend"]: {result["id"]: result["status"] for result in candidate["results"]}
        for candidate in report["candidates"]
    }
    sources = _case_sources(manifest)
    for case_id, upstream_tests in sources.items():
        lines.append(
            f"| `{case_id}` | {by_backend['python'][case_id]} | "
            f"{by_backend['rust'][case_id]} | "
            f"{'<br>'.join(f'`{source}`' for source in upstream_tests)} |"
        )
    lines.extend(
        [
            "",
            "## Adapted, excluded, and unsupported inventory",
            "",
            "| Status | Area | Upstream tests | Rationale |",
            "|---|---|---|---|",
        ]
    )
    for case in manifest["cases"]:
        tests = "<br>".join(f"`{source}`" for source in case["upstream_tests"])
        lines.append(f"| {case['status']} | {case['title']} | {tests} | {case['rationale']} |")
    lines.append("")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=ROOT / "conformance-results",
    )
    args = parser.parse_args()
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    baseline = _run_probe("scrapy", "python")
    expected_version = manifest["scrapy_version"]
    if baseline["environment"]["scrapy"] != expected_version:
        raise RuntimeError(
            f"expected Scrapy {expected_version}, found {baseline['environment']['scrapy']}"
        )
    sources = _case_sources(manifest)
    candidates = []
    failed = False
    for backend in ("python", "rust"):
        candidate = _run_probe("spideroxide", backend)
        results = _compare(baseline, candidate, sources)
        failed |= any(result["status"] == "fail" for result in results)
        candidates.append(
            {
                "backend": backend,
                "environment": candidate["environment"],
                "results": results,
            }
        )
    report = {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "baseline": {
            "implementation": baseline["implementation"],
            "environment": baseline["environment"],
        },
        "candidates": candidates,
        "inventory": {
            status: sum(case["status"] == status for case in manifest["cases"])
            for status in ("adapted", "excluded", "unsupported")
        },
    }
    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "conformance.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (args.output_dir / "conformance.md").write_text(
        _markdown(report, manifest),
        encoding="utf-8",
    )
    print(
        f"Conformance: {sum(len(candidate['results']) for candidate in candidates)} "
        f"backend checks, {'failed' if failed else 'all passed'}; reports in "
        f"{args.output_dir}"
    )
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
