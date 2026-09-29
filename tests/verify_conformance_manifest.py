from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "conformance" / "manifest.json"
PROBE = ROOT / "conformance" / "probe.py"


def _verify() -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    assert manifest["schema_version"] == 1
    assert manifest["scrapy_version"] == "2.19.0"
    assert manifest["scrapy_revision"] == manifest["scrapy_version"]
    cases = manifest["cases"]
    ids = [case["id"] for case in cases]
    assert len(ids) == len(set(ids))
    assert {"adapted", "excluded", "unsupported"} == {case["status"] for case in cases}
    for case in cases:
        assert case["title"]
        assert case["rationale"]
        assert case["upstream_tests"]
        assert all(source.startswith("tests/") for source in case["upstream_tests"])

    probe_source = PROBE.read_text(encoding="utf-8")
    for case in cases:
        if case["status"] == "adapted":
            assert f'"{case["id"]}"' in probe_source

    requirement = (ROOT / "requirements-conformance.txt").read_text(encoding="utf-8")
    assert requirement.strip() == f"Scrapy=={manifest['scrapy_version']}"


if __name__ == "__main__":
    _verify()
    print(
        "Conformance manifest passed: version pin, unique cases, statuses, "
        "upstream sources, rationales, and differential probe coverage"
    )
