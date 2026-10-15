# Third-party Scrapy integration certification

This suite exercises unchanged third-party packages through the Scrapy 2.19 compatibility
namespace on both SpiderOxide engines. `manifest.json` names each tested component, required
public APIs, scope, and limitation. `requirements-certification.txt` pins every selected package
and the test-only Redis and XLSX dependencies. The runner rejects missing or mismatched pins,
checks that subprocesses loaded the SpiderOxide shim rather than upstream Scrapy, and exits
nonzero on any changed certified behavior **or** unexpected blocked-case result.

From a checkout with Python 3.10 or 3.14 and a working Rust toolchain:

```bash
python -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/maturin develop --release
.venv/bin/python -m pip install -r requirements-twisted.txt
.venv/bin/python -m pip install -r requirements-certification.txt
.venv/bin/python certification/run.py --output-dir certification-results/local
```

The package resolver may install upstream Scrapy as a third-party dependency. Each probe runs in
an isolated subprocess with an explicit `PYTHONPATH` placing `compat/scrapy/src` ahead of it and
the repository's **test-only** conflict override. Do not use that override in deployments:
install `spideroxide-scrapy-compat` instead of upstream Scrapy in a separate environment.
No Redis server, Splash service, Playwright browser binary, proxy account, or network endpoint
is required. Redis uses pinned `fakeredis`; browser-service requests are rewritten locally.

`certification.json` and `certification.md` contain the exact Python, OS/platform, SpiderOxide,
and installed integration versions and both backend results. CI runs the suite on Ubuntu with
Python 3.10 and 3.14 and uploads both reports. The current matrix passes locally on macOS
with Python 3.14.6 on both backends; Python 3.10 and Ubuntu results must be read from their CI
artifacts, not inferred from that local run.

| Integration | Version | Tested behavior | Status and boundaries |
|---|---|---|---|
| `scrapy-deltafetch` | 2.1.0 | Real crawl persists an item fingerprint, then filters a request on the next crawl | Certified on both engines; required `StatsCollector.inc_value(..., spider=...)` and legacy fingerprint alias |
| `spidermon` | 1.27.0 | Real crawl validates an item, then rejects an invalid one | Validation pipeline certified; periodic `SpiderMonitorExtension` depends on Twisted `LoopingCall` and is not certified |
| `scrapy-redis` | 0.9.1 | Redis-backed duplicate filter canonicalizes requests and clears its store | Standalone component only; engines do not wire `DUPEFILTER_CLASS` |
| `scrapy-redis` item pipeline | 0.9.1 | A real crawl persists an item through unchanged `RedisPipeline` and `deferToThread` | Certified on both engines **with** `spideroxide[twisted]` and the opt-in asyncio reactor; repeated crawls must share its event loop |
| `scrapy-splash` | 0.11.1 | Splash request becomes a JSON POST with correct URL, args, and stats | Request/middleware path only with `slot_policy="single_slot"`; the default per-domain slot uses private `downloader._get_slot_key`, and an actual Splash service is not certified |
| `scrapy-xlsx` | 0.1.1 | Exporting a real item raises on `_get_serialized_fields` | Blocked by a private method removed from **upstream Scrapy 2.19 too**; the last release was in 2019 |
| `scrapy-zyte-smartproxy` | 2.4.1 | Importing the proxy middleware fails at `scrapy.resolver` | Blocked: a fake `dnscache` cannot track DNS results from the asyncio/Rust downloader |
| `scrapy-playwright` | 0.0.48 | The handler imports but rejects initialization without an opt-in reactor | Browser execution is not certified; no browser binary is installed |
| `scrapy-redis` item pipeline without reactor | 0.9.1 | `process_item` returns a Deferred that asyncio cannot complete without an installed reactor | Blocked without opt-in; cancel it rather than silently claiming an item was exported |

All selected packages declare permissive MIT or BSD licenses (see their PyPI project
metadata for redistribution terms). This suite certifies **the tested behavior**, not entire
distributions, external services, or unchanged operation of a Scrapy project using unsupported
settings. The private-method exporter and untested Twisted integrations remain explicit gaps instead of
being hidden by shim placeholders. `scrapy-rotating-proxies==0.6.2` was considered but not
selected: its last release predates supported Python versions and proxy-health checks depend on
Twisted `LoopingCall`.
