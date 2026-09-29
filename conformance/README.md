# Scrapy 2.19 conformance

This suite compares documented public behavior from Scrapy 2.19.0 with the SpiderOxide
compatibility namespace. The same probes run against upstream Scrapy, the Python crawl engine, and
the Rust crawl engine in isolated subprocesses so the two distributions never share the `scrapy`
package in one interpreter.

`manifest.json` is the machine-readable compatibility inventory. Every upstream test area is marked
as adapted, excluded, or unsupported and includes a rationale. Adapted entries point to
behavior-preserving differential probes. Excluded entries cover private implementation tests,
upstream-only test infrastructure, or public areas that still use repository-native adaptations.
Unsupported entries link the public gap to its roadmap issue.

Run the suite with the exact pinned upstream version:

```bash
python -m pip install -r requirements-conformance.txt
python conformance/audit.py
python conformance/run.py --output-dir conformance-results
```

The audit resolves the pinned Scrapy tag through the GitHub API and fails if any upstream test
module is unclassified, multiply classified, or matched by no inventory pattern. Set
`GITHUB_TOKEN` when unauthenticated API limits are exhausted. The runner writes:

- `conformance.json`, containing exact Scrapy, SpiderOxide, Python, and platform versions plus
  structured per-backend results.
- `conformance.md`, containing the human-readable compatibility matrix and exclusion inventory.

Any differential mismatch or unexpected Scrapy version exits non-zero. CI runs the suite after
installing Scrapy 2.19.0 and uploads both reports as build artifacts.
