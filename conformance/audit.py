from __future__ import annotations

import argparse
import fnmatch
import json
import os
import urllib.request
from pathlib import Path
from typing import Any

MANIFEST = Path(__file__).with_name("manifest.json")


def _matches(path: str, pattern: str) -> bool:
    if pattern.endswith("/"):
        return path.startswith(pattern)
    return fnmatch.fnmatchcase(path, pattern)


def _upstream_tests(repository: str, revision: str) -> list[str]:
    repository_path = repository.removeprefix("https://github.com/")
    request = urllib.request.Request(
        f"https://api.github.com/repos/{repository_path}/git/trees/{revision}?recursive=1",
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "SpiderOxide-conformance",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(request, timeout=30) as response:
        payload: dict[str, Any] = json.load(response)
    if payload.get("truncated"):
        raise RuntimeError("GitHub returned a truncated upstream tree")
    return sorted(
        entry["path"]
        for entry in payload["tree"]
        if entry["type"] == "blob"
        and entry["path"].startswith("tests/test_")
        and entry["path"].endswith(".py")
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, default=MANIFEST)
    args = parser.parse_args()
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    cases = manifest["cases"]
    patterns = [(pattern, case) for case in cases for pattern in case["upstream_tests"]]
    tests = _upstream_tests(
        manifest["scrapy_repository"],
        manifest["scrapy_revision"],
    )
    uncovered = [
        test for test in tests if not any(_matches(test, pattern) for pattern, _ in patterns)
    ]
    multiply_classified = {
        test: [
            case["id"]
            for case in cases
            if any(_matches(test, pattern) for pattern in case["upstream_tests"])
        ]
        for test in tests
    }
    multiply_classified = {
        test: case_ids for test, case_ids in multiply_classified.items() if len(case_ids) > 1
    }
    empty_patterns = [
        pattern for pattern, _ in patterns if not any(_matches(test, pattern) for test in tests)
    ]
    if uncovered or empty_patterns or multiply_classified:
        details = []
        if uncovered:
            details.append("Unclassified upstream tests:\n" + "\n".join(uncovered))
        if empty_patterns:
            details.append(
                "Inventory patterns matching no upstream tests:\n" + "\n".join(empty_patterns)
            )
        if multiply_classified:
            details.append(
                "Upstream tests with multiple classifications:\n"
                + "\n".join(
                    f"{test}: {', '.join(case_ids)}"
                    for test, case_ids in multiply_classified.items()
                )
            )
        raise RuntimeError("\n\n".join(details))
    by_status = {
        status: sum(
            any(
                case["status"] == status
                and any(_matches(test, pattern) for pattern in case["upstream_tests"])
                for case in cases
            )
            for test in tests
        )
        for status in ("adapted", "excluded", "unsupported")
    }
    print(
        f"Upstream inventory passed: {len(tests)} Scrapy "
        f"{manifest['scrapy_revision']} test modules classified "
        f"({by_status['adapted']} adapted matches, "
        f"{by_status['excluded']} excluded matches, "
        f"{by_status['unsupported']} unsupported matches)"
    )


if __name__ == "__main__":
    main()
