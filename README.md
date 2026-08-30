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
* Scrapy-compatible CSS, XPath, and JMESPath selectors
* Rust-owned retry policies and downloader statistics with Scrapy-compatible APIs
* Scrapy-compatible request depth limits, priorities, and statistics
* Downloader and spider middleware
* Item pipelines, signals, settings, and crawl statistics
* Streaming HTTP downloads with repeated header support
* Pooled asynchronous Rust HTTP downloader with HTTP/2 and Rustls
* Authenticated HTTP and HTTPS proxy routing with per-proxy connection pools
* Priority-ordered Scrapy-compatible extensions and lifecycle signals
* Streaming Scrapy-compatible JSON, JSON Lines, CSV, and XML feed exports
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
        for link in response.css("a"):
            yield {
                "text": link.xpath("string(.)").get(),
                "href": link.css("::attr(href)").get(),
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
filtering, concurrency admission, worker wakeups, start-request backpressure, idle detection, retry
decisions, robots policy state, and downloader attempt statistics into Rust. Spider callbacks,
middleware, pipelines, signals, and item objects remain in Python.

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

## Proxy support

SpiderOxide enables a Scrapy-compatible `HttpProxyMiddleware` by default. Set `proxy` in request
metadata to route one request through an HTTP or HTTPS proxy:

```python
from spideroxide import Request


request = Request(
    "https://example.com/private",
    meta={"proxy": "http://username:password@proxy.example.com:8080"},
)
```

Proxy credentials are removed from the normalized metadata URL and sent only as proxy
authentication. Redirected and retried requests retain the selected proxy. Set
`request.meta["proxy"]` to `None` to bypass proxy discovery for one request.

When no explicit proxy is present, SpiderOxide discovers standard `http_proxy`, `https_proxy`, and
`no_proxy` configuration from the environment and operating system. Set `HTTPPROXY_ENABLED` to
`False` to disable this behavior. `HTTPPROXY_AUTH_ENCODING` controls Basic authentication encoding
and defaults to `latin-1`.

Both downloaders pool connections separately for each proxy and authentication identity. The Rust
downloader owns its Reqwest proxy-client pool and applies `Proxy-Authorization` at the proxy layer so
credentials are not forwarded to direct target servers. SOCKS proxies are not currently supported.

## Extensions

Extensions are configured through the Scrapy-compatible `EXTENSIONS` setting. Values are numeric
priorities, and lower values are initialized first. Each extension can define `from_crawler()` to
read settings, retain crawler services, and connect synchronous or asynchronous signal handlers.

```python
from spideroxide import signals


class ItemCounterExtension:
    @classmethod
    def from_crawler(cls, crawler):
        extension = cls()
        extension.count = 0
        crawler.signals.connect(extension.item_scraped, signals.item_scraped)
        return extension

    def item_scraped(self, item, spider):
        self.count += 1


settings = {
    "EXTENSIONS": {
        ItemCounterExtension: 500,
    },
}
```

Extension references may be classes, import paths, or existing instances. Raising `NotConfigured`
from a factory skips that extension. `EXTENSIONS_BASE` provides framework defaults, and assigning
`None` to the same reference in `EXTENSIONS` disables a base extension. Loaded instances are
available from `crawler.extensions`, including iteration and `get_by_type()` inspection.

Extensions receive the same engine, spider, request, response, item, and error signals on both crawl
engines. With the Rust engine selected, scheduling and runtime policy state remain in Rust while
extensions run as Python observers through the public signal API.

## Feed exports

Configure one or more streaming outputs with Scrapy's `FEEDS` setting. SpiderOxide supports plain
filesystem paths, `file://` URIs, `pathlib.Path` keys, and `stdout:`. Built-in formats are `json`,
`jsonlines` with the `jsonl` and `jl` aliases, `csv`, and `xml`.

```python
settings = {
    "FEEDS": {
        "exports/%(name)s-%(time)s.jl": {
            "format": "jsonlines",
            "encoding": "utf-8",
            "fields": ["url", "title"],
            "overwrite": True,
        },
        "exports/%(name)s-%(batch_id)03d.csv": {
            "format": "csv",
            "fields": {
                "url": "URL",
                "title": "Title",
            },
            "batch_item_count": 10_000,
            "overwrite": True,
        },
    },
}
```

URI templates provide spider attributes plus `time`, `batch_time`, and `batch_id`. Batched feeds
must include `%(batch_id)` or `%(batch_time)s` so each batch has a distinct target. Feed options can
override `fields`, `encoding`, `indent`, `store_empty`, `overwrite`, `uri_params`,
`item_export_kwargs`, `item_classes`, and `item_filter`. The matching global defaults are
`FEED_EXPORT_FIELDS`, `FEED_EXPORT_ENCODING`, `FEED_EXPORT_INDENT`, `FEED_STORE_EMPTY`,
`FEED_EXPORT_BATCH_ITEM_COUNT`, and `FEED_URI_PARAMS`.

Filesystem feeds append by default for Scrapy compatibility. Set `overwrite` to `True` when each
crawl should replace the previous output. Export completion is observable through
`signals.feed_slot_closed` and `signals.feed_exporter_closed`; storage outcomes are recorded under
`feedexport/success_count/<Storage>` and `feedexport/failed_count/<Storage>`.

Custom formats and destinations can be registered through `FEED_EXPORTERS` and `FEED_STORAGES`.
The exporter runs as a Python extension because it serializes user-defined item types, while both
crawl engines retain their existing runtime ownership.

## Selectors

`TextResponse` provides the familiar Scrapy selector API through Parsel, the same selector library
used by Scrapy. CSS and XPath expressions can be chained and mixed, including `::text`,
`::attr(...)`, regular expressions, namespaces, and `get()` or `getall()` extraction.

```python
def parse(self, response):
    for product in response.css("article.product"):
        yield {
            "name": product.css("h2::text").get(),
            "price": product.xpath(".//span[@class='price']/text()").get(),
        }
```

JSON responses support JMESPath with `response.jmespath(...)`. Selector types are inferred from the
response `Content-Type`. Parsing uses the original response bytes, honors declared encodings and
byte order marks, and resolves the first HTML `<base href>` against the response URL.

## Retry policies

Transient download failures are retried automatically with the same policy on the Python and Rust
crawl engines. By default, SpiderOxide retries download exceptions and HTTP status codes `500`,
`502`, `503`, `504`, `522`, `524`, `408`, and `429` up to two times.

```python
from spideroxide import Request


request = Request(
    "https://example.com/api",
    meta={
        "max_retry_times": 4,
        "priority_adjust": -2,
    },
)
```

Set `dont_retry` in request metadata to disable retries for one request. Retried requests preserve
their callbacks, errbacks, headers, cookies, body, and metadata, bypass duplicate filtering, and
record `retry/count`, reason-specific counts, and exhaustion stats.

The retry policy is configured with `RETRY_ENABLED`, `RETRY_TIMES`, `RETRY_HTTP_CODES`,
`RETRY_PRIORITY_ADJUST`, `RETRY_EXCEPTIONS`, and `RETRY_GIVE_UP_LOG_LEVEL`. Spider callbacks and
middleware can also call `get_retry_request()` to use the same policy for application-level retry
decisions.

With `ENGINE_BACKEND` set to `rust`, `NativePolicyRuntime` owns status matching, retry counters,
limit and priority decisions, and raw request, response, and exception statistics. The Python
middleware preserves Python exception type identity and turns typed native decisions into the
public Scrapy-compatible request API. Native counter deltas are mirrored into `StatsCollector` so
extensions can safely contribute to the same statistics.

## Request depth policy

SpiderOxide enables a Scrapy-compatible `DepthMiddleware` by default. Start responses begin at depth
zero, requests yielded by their callbacks receive depth one, and each later callback increments the
depth again. The current value is available through `request.meta["depth"]` and
`response.meta["depth"]`.

Set `DEPTH_LIMIT` to the deepest request SpiderOxide should schedule. Its default value of `0` leaves
depth unlimited. SpiderOxide subtracts `depth * DEPTH_PRIORITY` from each generated request's
priority, so a positive value favors shallower requests and a negative value favors deeper requests.
For example:

```python
result = asyncio.run(
    Crawler(
        ExampleSpider,
        settings={
            "ENGINE_BACKEND": "rust",
            "DEPTH_LIMIT": 4,
            "DEPTH_PRIORITY": 1,
            "DEPTH_STATS_VERBOSE": True,
        },
    ).crawl()
)
```

Accepted child requests update `request_depth_max`. Enabling `DEPTH_STATS_VERBOSE` also records
`request_depth_count/<depth>` counters. With the Rust engine, `NativeDepthPolicy` owns depth
increments, limit decisions, arbitrary-size priority adjustments, and native statistics. Python
only maps each typed decision back to the Scrapy-compatible `Request` object.

## Download slots and AutoThrottle

The Rust engine applies Scrapy-compatible per-domain download slots before each network transfer.
`CONCURRENT_REQUESTS_PER_DOMAIN` limits simultaneous transfers to one domain, while
`DOWNLOAD_DELAY` and `RANDOMIZE_DOWNLOAD_DELAY` control the interval between transfer starts.
Requests can set `download_slot` in metadata to share a slot across domains or separate traffic
within one domain. `DOWNLOAD_SLOTS` can override `concurrency`, `delay`, and `randomize_delay` for
individual slot keys.

```python
result = asyncio.run(
    Crawler(
        ExampleSpider,
        settings={
            "ENGINE_BACKEND": "rust",
            "CONCURRENT_REQUESTS_PER_DOMAIN": 4,
            "DOWNLOAD_DELAY": 0.25,
            "DOWNLOAD_SLOTS": {
                "api.example.com": {"concurrency": 2, "delay": 0.5},
            },
        },
    ).crawl()
)
```

Enable adaptive delays with `AUTOTHROTTLE_ENABLED`. Rust starts each new slot at the greater of
`DOWNLOAD_DELAY` and `AUTOTHROTTLE_START_DELAY`, then adjusts its delay from measured network
latency and `AUTOTHROTTLE_TARGET_CONCURRENCY`. Adjustments stay between `DOWNLOAD_DELAY` and
`AUTOTHROTTLE_MAX_DELAY`. Fast non-200 responses never reduce the delay. Set
`autothrottle_dont_adjust_delay` to `True` in request metadata to exclude one response from
adjustment. `AUTOTHROTTLE_DEBUG` logs the current native slot state after each transfer.

Slot admission, active leases, timing, randomized intervals, adaptive delay decisions, and counters
remain in Rust. Python derives Scrapy-compatible slot keys, records `download_slot` and
`download_latency` in request metadata, and mirrors native counter deltas into public crawler stats.
These settings currently require `ENGINE_BACKEND` to be `rust` or a successful `auto` selection.
HTTP redirects return to the scheduler as Scrapy-compatible requests, so every redirect hop acquires
the correct domain slot and observes its concurrency and delay policy.

## Robots policy

Set `ROBOTSTXT_OBEY` to `True` with the Rust engine to enforce robots.txt before downloading a
request. Rust owns the per-origin policy cache, concurrent fetch deduplication, user-agent matching,
allow and deny decisions, waiter wakeups, failure state, and counters. Policy matching uses
Google-compatible longest-rule precedence with wildcard and end-anchor support.

```python
result = asyncio.run(
    Crawler(
        ExampleSpider,
        settings={
            "ENGINE_BACKEND": "rust",
            "ROBOTSTXT_OBEY": True,
            "ROBOTSTXT_USER_AGENT": "SpiderOxide",
        },
    ).crawl()
)
```

`ROBOTSTXT_USER_AGENT` overrides request headers for policy matching. Use a bare crawler product
token for the closest standards-compatible behavior. When it is unset, each request's `User-Agent`
header is used before falling back to `USER_AGENT`. Set `dont_obey_robotstxt` in request metadata to
bypass policy for one request.

The first request to an origin starts one `/robots.txt` transfer. Other concurrent requests for that
origin await the same native policy state. Policy transfers use the configured downloader, retry
middleware, redirects, native slots, and download statistics. A transfer failure is cached as
unavailable and fails open, matching Scrapy behavior. Any HTTP response body, including a non-200
response body, is parsed as policy content.

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
* CSS, XPath, XML namespace, JSON, malformed HTML, encoding, and base URL selector behavior
* retry status codes, exceptions, limits, opt-outs, request overrides, stats, and engine parity
* native policy ownership, exception inheritance, arbitrary priorities, fallback, and extension stats
* request depth limits, priority adjustments, verbose stats, arbitrary integers, and engine parity
* explicit and environment proxies, authentication, redirects, bypass rules, pools, and isolation
* extension priorities, overrides, factories, opt-outs, async hooks, lifecycle order, and parity
* feed formats, fields, encodings, templates, batches, filters, storage, signals, and engine parity

It covers normal and Unicode URLs, mixed case schemes and hosts, query ordering, duplicate query
parameters, fragments, default ports, request methods, bodies, priorities, and empty schedulers.

Run it with:

```bash
python benchmark/verify_correctness.py
python benchmark/verify_crawler.py
python benchmark/verify_extensions.py
python benchmark/verify_feed_exports.py
python benchmark/verify_native_downloader.py
python benchmark/verify_native_engine.py
python benchmark/verify_selectors.py
python benchmark/verify_retry.py
python benchmark/verify_native_policy.py
python benchmark/verify_depth.py
python benchmark/verify_native_slots.py
python benchmark/verify_redirect.py
python benchmark/verify_proxy.py
python benchmark/verify_native_robots.py
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
      <td>211.912 ms</td>
      <td>1,873.443 ms</td>
      <td><strong>8.84x</strong></td>
    </tr>
    <tr>
      <td>Duplicate filtering</td>
      <td>141.861 ms</td>
      <td>1,911.053 ms</td>
      <td><strong>13.47x</strong></td>
    </tr>
    <tr>
      <td>Scheduler insertion</td>
      <td>272.694 ms</td>
      <td>2,443.718 ms</td>
      <td><strong>8.96x</strong></td>
    </tr>
    <tr>
      <td>Scheduler removal</td>
      <td>220.222 ms</td>
      <td>408.325 ms</td>
      <td><strong>1.85x</strong></td>
    </tr>
    <tr>
      <td>Combined scheduler flow</td>
      <td>504.474 ms</td>
      <td>2,743.858 ms</td>
      <td><strong>5.44x</strong></td>
    </tr>
  </tbody>
</table>

At 10,000 requests, the combined scheduler flow completed in 42.266 ms for SpiderOxide and
271.284 ms for Scrapy, a 6.42x speedup.

The comparison used SpiderOxide 0.1.0, Scrapy 2.17.0, CPython 3.14.6, and Rust 1.97.1 on macOS
arm64 with 12 logical CPUs. Each value is the median of 10 measured runs after 3 warm-up runs.
Request construction was outside the timed sections, run order alternated, and garbage collection
ran before each timing.

These are component benchmarks. They exclude networking, response parsing, selectors, middleware,
pipelines, reactor overhead, and complete crawl behavior. They must not be interpreted as a 5.44x
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
python benchmark/verify_extensions.py
python benchmark/verify_feed_exports.py
python benchmark/verify_native_downloader.py
python benchmark/verify_native_engine.py
python benchmark/verify_selectors.py
python benchmark/verify_retry.py
python benchmark/verify_native_policy.py
python benchmark/verify_depth.py
python benchmark/verify_native_slots.py
python benchmark/verify_redirect.py
python benchmark/verify_proxy.py
python benchmark/verify_native_robots.py
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for contribution requirements.

## Status

SpiderOxide is experimental and does not yet provide full Scrapy compatibility. State is kept in
memory, and the URL canonicalizer intentionally implements a smaller contract than Scrapy.

The current foundation includes Python and Rust crawl coordination, HTTP models, Python and Rust
HTTP downloaders, spiders, middleware, item pipelines, signals, settings, stats, duplicate
filtering, scheduling, selectors, and Rust-owned retry, request depth, robots, download slot, and
downloader statistics policies. The native downloader also owns authenticated per-proxy connection
pools. Local and standard-output feed exports are available through the built-in feed extension.
SpiderOxide does not yet include persistent job state, remote feed storage, feed postprocessing,
built-in operational extensions, SOCKS proxy support, or Scrapy command-line compatibility.

The benchmark results support further integration work, but production adoption should be based on
representative crawls that include persistence, callbacks, crawl policies, and concurrency.

## License

SpiderOxide is available under the [MIT License](LICENSE).

SpiderOxide is an independent project and is not affiliated with or endorsed by Scrapy.
