![SpiderOxide](assets/spideroxide-logo.svg)

<p align="center">
  <a href="https://www.python.org/">
    <img alt="Python 3.10+" src="https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white">
  </a>
  <a href="https://www.rust-lang.org/">
    <img alt="Rust stable" src="https://img.shields.io/badge/Rust-stable-CE422B?logo=rust&logoColor=white">
  </a>
  <a href="https://pyo3.rs/">
    <img alt="PyO3" src="https://img.shields.io/badge/PyO3-0.25-FFD43B">
  </a>
  <a href="LICENSE">
    <img alt="MIT License" src="https://img.shields.io/badge/license-MIT-26394A">
  </a>
</p>

SpiderOxide is an experimental Python web crawling framework with optional Rust acceleration. It
provides an asynchronous crawl engine, request and response models, middleware, item pipelines,
signals, settings, statistics, duplicate filtering, and priority scheduling. The native
request-processing backend is exposed to Python with PyO3.

The project was created to find out whether these crawler hot paths benefit from a native
implementation. Both backends follow the same behavioral contract and are checked against the
same deterministic data before performance is measured.

SpiderOxide is inspired by Scrapy, but it does not modify or replace Scrapy.

## Features

* Deterministic SHA-256 request fingerprints
* URL canonicalization with duplicate query parameter support
* In-memory duplicate filtering
* Stable priority scheduling
* FIFO ordering for requests with equal priority
* Single-request and batch APIs
* Runtime selection of the Python or Rust backend
* Preservation of the original Python request object
* Concurrent asynchronous crawl engine
* Native Tokio crawl coordination with bounded start-request admission
* Downloader and spider middleware
* Item pipelines, signals, settings, and crawl statistics
* Streaming HTTP downloads with repeated header support
* Pooled asynchronous Rust HTTP downloader with HTTP/2 and Rustls
* Safe Rust with no unsafe blocks

## Installation

SpiderOxide requires Python 3.10 or newer, stable Rust, and a native build toolchain.

From a source checkout:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
maturin develop -r
```

On Windows PowerShell, activate the environment with:

```powershell
.\.venv\Scripts\Activate.ps1
```

## Crawling

A spider defines its initial URLs and parses responses into requests or items.

```python
import asyncio

from spideroxide import Crawler, Spider


class ExampleSpider(Spider):
    name = "example"
    start_urls = ["https://example.com/"]

    def parse(self, response):
        yield {
            "url": response.url,
            "status": response.status,
        }


result = asyncio.run(Crawler(ExampleSpider).crawl())
print(result.items)
print(result.stats)
```

`Spider.start()` may also be implemented as an asynchronous generator. Requests begin downloading
as they are yielded, so startup does not need to finish before callbacks run.

The HTTPX downloader is the default. Select the native Rust downloader explicitly:

```python
result = asyncio.run(
    Crawler(
        ExampleSpider,
        settings={"DOWNLOADER_BACKEND": "rust"},
    ).crawl()
)
```

The native downloader uses a shared Reqwest connection pool, Rustls, streamed response bodies,
automatic decompression, and the same timeout, response-size, redirect, header, cookie, and response
contracts as the Python downloader. Use `auto` to prefer Rust and fall back to HTTPX when the native
extension is unavailable.

The Python crawl engine is also the default. The native engine moves priority scheduling, duplicate
filtering, concurrency admission, worker wakeups, start-request backpressure, and idle detection into
Rust. Spider callbacks, middleware, pipelines, signals, and item objects remain in Python.

Select the complete native runtime explicitly:

```python
result = asyncio.run(
    Crawler(
        ExampleSpider,
        settings={
            "ENGINE_BACKEND": "rust",
            "DOWNLOADER_BACKEND": "rust",
        },
    ).crawl()
)
```

`ENGINE_BACKEND` accepts `python`, `rust`, or `auto`. `ENGINE_MAX_PENDING` bounds queued start
requests and defaults to twice `CONCURRENT_REQUESTS` when set to `0`.

## Request processing

SpiderOxide accepts any request object with `url`, `method`, `body`, and `priority` attributes.

```python
from dataclasses import dataclass

from spideroxide import DupeFilter, Scheduler, fingerprint_request


@dataclass
class Request:
    url: str
    method: str = "GET"
    body: bytes = b""
    priority: int = 0


request = Request("https://example.com/products?sort=price", priority=100)

fingerprint = fingerprint_request(request, backend="rust")

dupe_filter = DupeFilter(backend="rust")
assert dupe_filter.seen_request(request) is False
assert dupe_filter.seen_request(request) is True

scheduler = Scheduler(backend="rust")
assert scheduler.push_request(request) is True
assert scheduler.pop() is request
```

Python is the default backend. Set `SCRAPY_RUST_BACKEND` to `rust` to require the native backend,
or to `auto` to use Rust when it is available and Python otherwise.

```bash
export SCRAPY_RUST_BACKEND=rust
```

Explicit Rust selection fails if the extension is unavailable. It does not silently fall back to
Python.

## Correctness

The validation suite compares:

* fingerprints byte for byte
* duplicate filter decisions
* scheduler insertion decisions
* complete scheduler output order
* FIFO behavior at equal priorities
* original request object identity
* asynchronous spider lifecycle and cleanup
* middleware and item pipeline behavior
* callback and errback handling
* native HTTP methods, bodies, repeated headers, and cookies
* native redirects, compression, streaming, timeouts, and response limits
* native priority ordering, duplicate decisions, and request identity
* native concurrency, backpressure, streaming starts, cancellation, and cleanup

It covers normal and Unicode URLs, mixed case schemes and hosts, query ordering, duplicate query
parameters, fragments, default ports, request methods, bodies, priorities, and empty schedulers.

Run it with:

```bash
python benchmark/verify_correctness.py
python benchmark/verify_crawler.py
python benchmark/verify_native_downloader.py
python benchmark/verify_native_engine.py
```

The standard validation uses 10,000 deterministic requests with a fixed random seed.

## Benchmarks

### SpiderOxide compared with Scrapy

SpiderOxide was compared with Scrapy 2.17.0 using real Scrapy requests,
`RFPDupeFilter`, and Scrapy's `Scheduler` configured with its standard priority queue and FIFO
in-memory queues. The SpiderOxide Rust backend and Scrapy processed the same deterministic workload
with 70,000 unique requests and 30,000 duplicates.

<table>
  <thead>
    <tr>
      <th>100,000 requests</th>
      <th>SpiderOxide</th>
      <th>Scrapy</th>
      <th>Speedup</th>
    </tr>
  </thead>
  <tbody>
    <tr>
      <td>Fingerprinting</td>
      <td>208.984 ms</td>
      <td>1,837.775 ms</td>
      <td><strong>8.79x</strong></td>
    </tr>
    <tr>
      <td>Duplicate filtering</td>
      <td>140.853 ms</td>
      <td>1,876.409 ms</td>
      <td><strong>13.32x</strong></td>
    </tr>
    <tr>
      <td>Scheduler insertion</td>
      <td>272.259 ms</td>
      <td>2,424.178 ms</td>
      <td><strong>8.90x</strong></td>
    </tr>
    <tr>
      <td>Scheduler removal</td>
      <td>214.439 ms</td>
      <td>389.061 ms</td>
      <td><strong>1.81x</strong></td>
    </tr>
    <tr>
      <td>Combined scheduler flow</td>
      <td>505.125 ms</td>
      <td>2,716.902 ms</td>
      <td><strong>5.38x</strong></td>
    </tr>
  </tbody>
</table>

The comparison used SpiderOxide 0.1.0, Scrapy 2.17.0, CPython 3.14.6, and Rust 1.97.1 on macOS
arm64 with 12 logical CPUs. Each value is the median of 10 measured runs after 3 warm-up runs.
Request construction was outside the timed sections, run order alternated, and garbage collection
ran before each timing.

These are component benchmarks. They exclude networking, response parsing, selectors, middleware,
pipelines, reactor overhead, and complete crawl behavior. They must not be interpreted as a 5.38x
end-to-end Scrapy crawl speedup.

The complete 10,000 and 100,000 request results are available in the
[Scrapy comparison report](results/scrapy_comparison.md),
[JSON data](results/scrapy_comparison.json), and
[CSV data](results/scrapy_comparison.csv).

Install the comparison dependency and reproduce the benchmark with:

```bash
python -m pip install -r requirements-benchmark.txt
python benchmark/benchmark_scrapy.py
```

### Python reference benchmark

SpiderOxide also includes the original Python reference implementation used to verify the native
algorithm in isolation. Its full results are available in the
[reference report](results/report.md), [JSON data](results/results.json), and
[CSV data](results/results.csv).

Run the Python reference benchmark with:

```bash
python benchmark/run_all.py
```

## Development

Format and check the Python code:

```bash
python -m ruff format .
python -m ruff check .
```

Format and check the Rust crate:

```bash
cd rust_impl
cargo fmt
cargo clippy -r
```

Build the extension and run all validation scripts:

```bash
maturin develop -r
python benchmark/verify_integration.py
python benchmark/verify_correctness.py
python benchmark/verify_crawler.py
python benchmark/verify_native_downloader.py
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for contribution requirements.

## Status

SpiderOxide is experimental and does not yet provide full Scrapy compatibility. State is kept in
memory, and the URL canonicalizer intentionally implements a smaller contract than Scrapy.

The current foundation includes Python and Rust crawl coordination, HTTP models, Python and Rust
HTTP downloaders, spiders, middleware, item pipelines, signals, settings, stats, duplicate
filtering, and scheduling. It does not yet include selector APIs, persistent job state, feed
exports, extensions, robots handling, retry policies, proxy support, throttling, request depth
policies, or Scrapy command-line compatibility.

The benchmark results support further integration work, but production adoption should be based on
representative crawls that include persistence, callbacks, retries, and concurrency.

## License

SpiderOxide is available under the [MIT License](LICENSE).

SpiderOxide is an independent project and is not affiliated with or endorsed by Scrapy.
