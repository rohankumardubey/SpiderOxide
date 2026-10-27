# Installation and distribution

[Back to overview](../README.md)

## Quick start

SpiderOxide targets GIL-enabled CPython 3.10 through 3.14. Building from source requires stable
Rust and a native build toolchain; installing a compatible wheel does not.

### Distribution support

| Target | CI wheel build and clean-install crawl | Validation status |
| --- | --- | --- |
| Linux x86-64, Linux ARM64 (manylinux 2.28) | CPython 3.10–3.14 | All 10 targets passed CI |
| macOS ARM64 (macOS 11+) | CPython 3.10–3.14 | All five targets passed CI |
| macOS x86-64 (macOS 11+) | CPython 3.10–3.14 | All five targets passed CI |
| Windows x86-64 | CPython 3.10–3.14 | All five targets passed CI |
| Windows ARM64 and other architectures | Not built | Not claimed |

Each CI wheel target builds the native and Scrapy-compatibility distributions and checks isolated
imports, Python/Rust crawls, the `scrapy` executable, and uninstall. A separate Linux job compares
two source builds and two native wheels byte-for-byte, rebuilds wheels from the source archive,
and repeats the installed smoke test. CI builds pin maturin and Rust and use `Cargo.lock`;
maturin's auto-generated Rust SBOM is disabled because its random UUID and build timestamp make
otherwise identical wheels differ. Optional extras and a separate upstream-Scrapy third-party
environment (with the pinned certification packages) are exercised on Linux CPython 3.14. All
optional dependency groups are checked in separate clean environments. These targets are **not
confirmed supported until their CI jobs pass**. All 25 targets and the source-distribution
reproducibility job passed in
[this CI run](https://github.com/rohankumardubey/SpiderOxide/actions/runs/40504234725).
When the matrix and source job both pass, CI validates all 25 platform-specific wheel tags and
contents, then packages the native wheels, one pure-Python shim wheel, source archive, and a
`SHA256SUMS` manifest as a `release-candidate` artifact. The assembly passed in
[this CI run](https://github.com/rohankumardubey/SpiderOxide/actions/runs/40508315291).
This does not publish a release.
To prepare a release, first create a version tag (for example `v0.1.0`) on the exact commit
whose **main-branch CI** run produced a successful `release-candidate` artifact. Dispatch
**Prepare draft release** from `main` with that tag and CI run ID. It checks the tag/commit/run
match, verifies all 27 artifact checksums and wheel contents, and smoke-tests the installed
Linux CPython 3.10 wheels. If a previous published release exists, it also installs that
release's wheels in a clean environment and upgrades to the candidate before creating a **draft**
GitHub release. For the first release, this upgrade test is skipped; a real previous-version
upgrade cannot be certified until there is an earlier published release. Review and publish the
draft separately; the workflow neither creates tags nor publishes releases automatically.
The Linux CPython 3.14 job also tests removing upstream Scrapy before installing the compatible
`scrapy` namespace and command; this migration is separate from a previous-release upgrade.
Manually dispatch CI with `release_tag` to smoke-test the published Linux x86-64 CPython 3.10
wheels and source distribution, or with `previous_tag` to upgrade from an earlier published
release's wheels to the current CI-built wheels. No prior release exists yet, so a real version
upgrade cannot currently be certified.

From a source checkout:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m maturin develop --release
```

On Windows PowerShell, activate the environment with:

```powershell
.\.venv\Scripts\Activate.ps1
```

### Scrapy-compatible imports

The optional `spideroxide-scrapy-compat` distribution exposes implemented SpiderOxide APIs through
Scrapy 2.19 import paths, allowing compatible projects to keep imports such as
`from scrapy import Spider, Request`.

From a source checkout after building `spideroxide` above, in an environment without upstream
Scrapy:

```bash
python -m pip install ./compat/scrapy
```

Once both distributions have been published, install them with
`python -m pip install spideroxide spideroxide-scrapy-compat` instead.

Upstream Scrapy and the compatibility distribution both own the `scrapy` package and must be
installed in separate environments. The main `spideroxide` distribution never claims that
namespace, so it remains safe to install beside upstream Scrapy for differential testing. See
[`compat/scrapy/COMPATIBILITY.md`](../compat/scrapy/COMPATIBILITY.md) for the supported module surface,
public roadmap gaps, and explicitly unsupported private internals.

The compatibility distribution also installs the `scrapy` executable. Existing projects can run
`scrapy crawl`, `scrapy runspider`, `scrapy shell`, and the other Scrapy 2.19 commands unchanged;
new projects and spiders can be created with `scrapy startproject` and `scrapy genspider`.

```bash
scrapy startproject example
cd example
scrapy genspider catalog example.com
scrapy crawl catalog -O items.jsonl:jsonlines
```

Spider callback docstrings may keep Scrapy contracts, including custom classes registered through
`SPIDER_CONTRACTS`. Run them across the project or for one spider with:

```bash
scrapy check
scrapy check catalog
```

### Scrapy conformance

The version-pinned [conformance suite](../conformance/README.md) replays documented public behavior
against Scrapy 2.19.0 and SpiderOxide's Python and Rust engines in isolated subprocesses. It covers
HTTP models, request identity, settings, selectors and items, spider utilities, runtime APIs,
command metadata, and a real data-URI crawl.

```bash
python -m pip install -r requirements-conformance.txt
python conformance/audit.py
python conformance/run.py --output-dir conformance-results
```

`conformance/manifest.json` tracks adapted, excluded, and unsupported upstream test areas with a
rationale for every exclusion. Each CI matrix job publishes machine-readable JSON and a Markdown
compatibility report containing the exact Scrapy, SpiderOxide, Python, and platform versions.
Differential mismatches fail the job.
