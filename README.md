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
* Rust-native persistent crawl state with crash recovery
* Rust-accelerated Scrapy-compatible link extraction and CrawlSpider rules
* Scrapy-compatible CSS, XPath, and JMESPath selectors
* Scrapy-compatible Items, field metadata, Item Loaders, and processors
* Rust-owned retry policies and downloader statistics with Scrapy-compatible APIs
* Scrapy-compatible request depth limits, priorities, and statistics
* Downloader and spider middleware
* Item pipelines, signals, settings, and crawl statistics
* Scrapy-compatible cookie middleware with isolated native cookie jars
* Persistent Scrapy-compatible HTTP caching with native SQLite storage
* Scrapy-compatible file and image pipelines with native persistent storage
* Streaming HTTP downloads with repeated header support
* Pooled asynchronous Rust HTTP downloader with HTTP/2 and Rustls
* Authenticated HTTP and HTTPS proxy routing with per-proxy connection pools
* Priority-ordered Scrapy-compatible extensions and lifecycle signals
* Streaming Scrapy-compatible JSON, JSON Lines, CSV, and XML feed exports
* FTP, S3, and GCS feed storage with gzip, bzip2, and LZMA postprocessing
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

## Items and Item Loaders

`Item` provides a mapping with an explicit field schema. `Field` metadata can configure loader input
and output processors, serializers, or application-specific values. Undeclared fields raise
`KeyError`, which catches schema mistakes before items reach pipelines or feed exporters.

```python
from spideroxide import Field, Item, ItemLoader, Join, MapCompose, TakeFirst


class Product(Item):
    name = Field(input_processor=MapCompose(str.strip), output_processor=TakeFirst())
    price = Field(input_processor=MapCompose(float), output_processor=TakeFirst())
    tags = Field(output_processor=Join(","))


def parse_product(response):
    loader = ItemLoader(item=Product(), response=response)
    loader.add_css("name", "h1::text")
    loader.add_css("price", ".price::attr(data-value)")
    loader.add_value("tags", ["catalog", "featured"])
    return loader.load_item()
```

Loaders support direct values, CSS, XPath, and JMESPath extraction; add and replace operations;
nested selectors; loader contexts; and per-loader, per-field, or default processors. `MapCompose`,
`Compose`, `TakeFirst`, `Join`, `Identity`, and `SelectJmes` follow the Item Loaders processing
contract. The shared `ItemAdapter` also supports dictionaries, dataclasses, attrs classes, and
Pydantic models, so those item types use the same loader and pipeline path.

## File and image pipelines

`FilesPipeline` and `ImagesPipeline` download media yielded in item URL fields without sending those
requests through the scheduler. Downloads still pass through downloader middleware, including
cookies, redirects, retries, proxies, caching, and native download slots. Requests with the same
fingerprint share one crawl-lifetime result, including failures.

```python
from spideroxide import Spider


class ProductSpider(Spider):
    name = "products"
    start_urls = ["https://example.com/products"]

    def parse(self, response):
        yield {
            "file_urls": ["https://example.com/manual.pdf"],
            "image_urls": ["https://example.com/photo.png"],
        }


settings = {
    "ITEM_PIPELINES": {
        "spideroxide.pipelines.FilesPipeline": 100,
        "spideroxide.pipelines.ImagesPipeline": 200,
    },
    "FILES_STORE": "downloads/files",
    "IMAGES_STORE": "downloads/images",
    "IMAGES_THUMBS": {"small": (120, 120)},
}
```

Files are stored under `full/<sha1><extension>`. Images are validated, EXIF-transposed, converted to
RGB JPEG when needed, and stored under `full/<sha1>.jpg`; thumbnails use
`thumbs/<name>/<sha1>.jpg`. Results contain `url`, `path`, `checksum`, and a `downloaded`, `cached`,
or `uptodate` status. Local persistence, atomic replacement, MD5 checksums, and freshness metadata
are handled by `NativeMediaStore`.

Set `FILES_URLS_FIELD`, `FILES_RESULT_FIELD`, `FILES_EXPIRES`, `IMAGES_URLS_FIELD`,
`IMAGES_RESULT_FIELD`, `IMAGES_EXPIRES`, `IMAGES_MIN_WIDTH`, `IMAGES_MIN_HEIGHT`, and
`IMAGES_THUMBS` to customize behavior. Redirects are rejected by default; enable
`MEDIA_ALLOW_REDIRECTS` when media endpoints redirect. Image support requires
`pip install "spideroxide[images]"`. The built-in media stores currently support local paths and
`file://` URLs.

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

## CrawlSpider and link extraction

`LinkExtractor` and `LxmlLinkExtractor` provide Scrapy-compatible URL, domain, extension, text,
tag, attribute, CSS, and XPath filtering. Full valid HTML documents use the native Rust parser for
candidate extraction. Restricted regions and malformed documents use the lxml-backed compatibility
path so observable results remain stable.

```python
from spideroxide import CrawlSpider, LinkExtractor, Rule


class CatalogSpider(CrawlSpider):
    name = "catalog"
    start_urls = ["https://example.com/catalog"]
    rules = (
        Rule(
            LinkExtractor(
                allow=r"/products/",
                allow_domains="example.com",
                restrict_css="main",
            ),
            callback="parse_product",
            follow=True,
        ),
    )

    async def parse_product(self, response):
        return {
            "url": response.url,
            "name": response.css("h1::text").get(),
        }
```

Rules support callback and errback method names, callback keyword arguments, `process_links`,
`process_request`, recursive following, and the `CRAWLSPIDER_FOLLOW_LINKS` setting. Generated
requests retain their rule index and link text in request metadata and remain serializable through
`JOBDIR`.

## Spider middleware

Configure spider middleware with `SPIDER_MIDDLEWARES`. Lower priority components receive responses
first through `process_spider_input`. Output and exception hooks run in reverse order. Both crawl
engines support synchronous and asynchronous hooks, including the asynchronous
`process_spider_output_async` hook and streaming `process_start` hook.

```python
class AuditMiddleware:
    async def process_start(self, start):
        async for output in start:
            yield output

    def process_spider_input(self, response, spider):
        spider.logger.info("Parsing %s", response.url)

    def process_spider_output(self, response, result, spider):
        yield from result

    def process_spider_exception(self, response, exception, spider):
        return None


settings = {
    "SPIDER_MIDDLEWARES": {
        AuditMiddleware: 500,
    },
}
```

The legacy `process_start_requests` hook remains supported for spiders that use the default
synchronous `start_requests()` source while the middleware chain remains synchronous. Middleware
consuming a custom asynchronous `Spider.start()` or following a modern `process_start`
transformation must implement `process_start` instead.

Request errbacks receive failures from spider input and callbacks before spider exception
middleware. Successful errback output then passes through spider output middleware. Output
middleware failures skip request errbacks and continue at the next eligible spider exception hook.
Items yielded before a callback or middleware generator fails are retained and continue through the
remaining middleware chain.

## Cookies

Cookie handling is enabled by default through `CookiesMiddleware`. Native cookie jars apply
Scrapy-compatible domain, path, secure, expiration, and public-suffix rules for both downloader
backends. Downloader clients remain stateless, so cookies cannot leak around middleware jar
selection or bypass rules.

```python
yield Request(
    "https://example.com/account",
    cookies={"session": "value"},
    meta={"cookiejar": "account"},
)
```

`Request.cookies` accepts a name and value mapping or Scrapy's verbose cookie list with `name`,
`value`, `domain`, `path`, and `secure` fields. Use the same `cookiejar` metadata value on later
requests to keep an isolated session. Metadata is not copied automatically when following links.
Set `dont_merge_cookies` to bypass both stored and request cookies while preserving a manually
provided `Cookie` header. Set `COOKIES_ENABLED` to `False` to disable middleware processing, or
enable `COOKIES_DEBUG` to log sent and received cookie headers.

## HTTP cache

Enable persistent response caching with `HTTPCACHE_ENABLED`. The default `DummyPolicy` reuses every
stored response except ignored schemes and status codes. `RFC2616Policy` honors HTTP freshness,
validators, `Cache-Control`, `Date`, `Age`, `Expires`, and `Last-Modified`; stale entries are
revalidated with conditional request headers and may recover eligible server or download failures.

```python
settings = {
    "HTTPCACHE_ENABLED": True,
    "HTTPCACHE_DIR": ".cache/http",
    "HTTPCACHE_POLICY": "spideroxide.httpcache.RFC2616Policy",
    "HTTPCACHE_EXPIRATION_SECS": 86400,
}
```

`NativeHttpCacheStorage` keeps each spider's cache in a SQLite WAL database. Cache keys use the
native request fingerprint, so the method, canonical URL, and request body participate in identity.
Response URLs, status codes, bodies, and repeated headers survive process restarts. The standard
response type is reconstructed from the cached metadata, and cache hits include `cached` in
`Response.flags`.

The supported compatibility settings are `HTTPCACHE_STORAGE`, `HTTPCACHE_IGNORE_HTTP_CODES`,
`HTTPCACHE_IGNORE_MISSING`, `HTTPCACHE_IGNORE_SCHEMES`, `HTTPCACHE_ALWAYS_STORE`, and
`HTTPCACHE_IGNORE_RESPONSE_CACHE_CONTROLS`. Set request metadata `dont_cache` to bypass lookup and
storage. `HTTPCACHE_GZIP` and `HTTPCACHE_DBM_MODULE` are accepted for settings compatibility but do
not change native SQLite storage.

## Scheduler queues

Both crawl engines support Scrapy's built-in FIFO and LIFO queue settings. Normal requests use LIFO
queues by default, while start requests use separate FIFO queues. Higher priorities are dequeued
first. At equal priority, normal requests are dequeued before start requests.

```python
settings = {
    "SCHEDULER_MEMORY_QUEUE": "scrapy.squeues.FifoMemoryQueue",
    "SCHEDULER_DISK_QUEUE": "scrapy.squeues.PickleFifoDiskQueue",
    "SCHEDULER_START_MEMORY_QUEUE": None,
    "SCHEDULER_START_DISK_QUEUE": None,
}
```

Setting both start queue values to `None` places start requests in the normal queues. The disk
settings apply to serializable requests stored through `JOBDIR`. Memory-only requests are dequeued
before persisted requests, matching Scrapy. Marshal and Pickle FIFO and LIFO disk queue paths are
accepted. Unsupported custom queue classes fail during engine construction instead of being
silently approximated.

The public `Scheduler` component retains FIFO ties by default so existing component benchmarks and
standalone callers keep their established behavior. Crawl engines use the Scrapy-compatible settings
above.

## Persistent jobs

Set `JOBDIR` with the Rust engine to make a crawl resumable. The native coordinator stores queued
and in-flight requests, duplicate fingerprints, priorities, sequence numbers, start-request
classification, and spider state in a locked SQLite WAL. A restarted crawler restores the frontier
before it begins producing new start requests.

```python
result = asyncio.run(
    Crawler(
        ExampleSpider,
        settings={
            "ENGINE_BACKEND": "rust",
            "DOWNLOADER_BACKEND": "rust",
            "JOBDIR": "crawls/example-1",
        },
    ).crawl()
)
```

`ENGINE_BACKEND="auto"` also selects the native engine when `JOBDIR` is configured and does not
fall back to the Python engine. Only one crawler may own a job directory at a time. Reusing a
directory for a different spider or running two processes against it is unsupported.

Serializable requests survive normal cancellation, process termination, and in-flight interruption.
Callback and errback functions must be bound methods of the running spider so they can be restored
by name. Request headers, bodies, cookies, metadata, callback keyword arguments, flags, duplicate
decisions, arbitrary-size priorities, and configured queue order are retained.

When a request cannot be serialized, it remains in memory for the current process and increments
`scheduler/unserializable`, matching Scrapy's fallback behavior. Set `SCHEDULER_DEBUG=True` to log
the first such request. Memory-only requests do not survive interruption.

With `JOBDIR` enabled, `spider.state` is a dictionary restored before the crawl starts and saved
after the spider closes. The directory contains Python pickle payloads for user-defined request
values, so it must be treated with the same trust as source code. Start a new job directory after
changing incompatible callback names or upgrading across incompatible SpiderOxide versions.

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
filesystem paths, `file://` URIs, `pathlib.Path` keys, `stdout:`, FTP, Amazon S3, and Google Cloud
Storage. Built-in formats are `json`, `jsonlines` with the `jsonl` and `jl` aliases, `csv`, and
`xml`.

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
        "s3://crawler-results/%(name)s/items.jl.gz": {
            "format": "jsonlines",
            "overwrite": True,
            "postprocessing": [
                "spideroxide.feedpostprocessing.GzipPlugin",
            ],
            "gzip_compresslevel": 6,
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

Remote feeds use temporary local files and upload them outside the event loop. Configure the
temporary directory with `FEED_TEMPDIR` and bound simultaneous uploads with
`FEED_STORAGE_CONCURRENCY`, which defaults to four. FTP credentials may be embedded in the URI, and
`FEED_STORAGE_FTP_ACTIVE` selects active mode. S3 uses the standard AWS credential chain or the
`AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_SESSION_TOKEN`, `AWS_ENDPOINT_URL`, and
`AWS_REGION_NAME` settings. GCS uses application default credentials and `GCS_PROJECT_ID`. Optional
object ACLs come from `FEED_STORAGE_S3_ACL` and `FEED_STORAGE_GCS_ACL`.

Install S3 support with `pip install "spideroxide[s3]"`, GCS support with
`pip install "spideroxide[gcs]"`, or both with `pip install "spideroxide[remote-feeds]"`. FTP and
the built-in postprocessors require no additional dependencies. Add `GzipPlugin`, `Bz2Plugin`, or
`LZMAPlugin` to a feed's `postprocessing` list and pass the matching `gzip_*`, `bz2_*`, or `lzma_*`
options to control compression.

Custom formats and destinations can be registered through `FEED_EXPORTERS` and `FEED_STORAGES`.
The exporter runs as a Python extension because it serializes user-defined item types, while both
crawl engines retain their existing runtime ownership.

## HTTP models

SpiderOxide provides Scrapy-compatible `Request`, `FormRequest`, and `JsonRequest` classes. Requests
support Unicode URL quoting, text or byte bodies, repeated headers, callbacks, errbacks, metadata,
callback keyword arguments, flags, copying, replacement, dictionary serialization, and common curl
commands.

```python
from spideroxide import FormRequest, JsonRequest, Request


page = Request.from_curl("curl https://example.com/private -u user:secret -H 'X-Trace: crawl'")
login = FormRequest(
    "https://example.com/login",
    formdata={"username": "reader", "password": "secret"},
)
api = JsonRequest(
    "https://example.com/api/items",
    data={"limit": 100, "active": True},
)
```

`Response`, `TextResponse`, `HtmlResponse`, and `XmlResponse` provide copy and replacement helpers,
request metadata, callback keyword arguments, URL joining, `follow()`, and `follow_all()`.
`TextResponse.follow_all()` accepts URL iterables, CSS expressions, XPath expressions, and selector
lists. `FormRequest.from_response()` supports common HTML form controls and explicit submit-button
selection.

The downloader selects response subclasses from `Content-Type`, URL extensions, and conservative
body inspection. Built-in request subclasses retain their type and configuration when restored from
`JOBDIR`.

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
* FIFO and LIFO crawl queues, start-request precedence, storage precedence, and recovery
* original request object identity
* asynchronous spider lifecycle and cleanup
* middleware and item pipeline behavior
* callback and errback handling
* HTTP headers, request serialization, curl, forms, JSON, response subclasses, and following
* cookie domains, paths, security, expiration, isolated jars, bypasses, and transport parity
* persistent cache hits, expiration, revalidation, repeated headers, lifecycle, and engine parity
* native HTTP methods, bodies, repeated headers, and cookies
* native redirects, compression, streaming, timeouts, and response limits
* native priority ordering, duplicate decisions, and request identity
* native concurrency, backpressure, streaming starts, cancellation, and cleanup
* native job locking, WAL recovery, callbacks, spider state, hard crashes, and schema checks
* CSS, XPath, XML namespace, JSON, malformed HTML, encoding, and base URL selector behavior
* item schemas, field metadata, adapters, loaders, processors, nesting, and engine parity
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
python benchmark/verify_crawl_spider.py
python benchmark/verify_spider_middleware.py
python benchmark/verify_http_models.py
python benchmark/verify_cookies.py
python benchmark/verify_http_cache.py
python benchmark/verify_extensions.py
python benchmark/verify_feed_exports.py
python benchmark/verify_native_downloader.py
python benchmark/verify_native_engine.py
python benchmark/verify_scheduler_queues.py
python benchmark/verify_job_state.py
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
python benchmark/verify_crawl_spider.py
python benchmark/verify_spider_middleware.py
python benchmark/verify_http_models.py
python benchmark/verify_cookies.py
python benchmark/verify_http_cache.py
python benchmark/verify_extensions.py
python benchmark/verify_feed_exports.py
python benchmark/verify_native_downloader.py
python benchmark/verify_native_engine.py
python benchmark/verify_scheduler_queues.py
python benchmark/verify_job_state.py
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

SpiderOxide is experimental and does not yet provide full Scrapy compatibility. Crawls can persist
native scheduling state through `JOBDIR`, while crawl results and statistics remain in memory. The
URL canonicalizer intentionally implements a smaller contract than Scrapy.

The current foundation includes Python and Rust crawl coordination, Scrapy-compatible request and
response classes, Python and Rust HTTP downloaders, spiders, middleware, structured Items, Item
Loaders, item pipelines, signals, settings, stats, duplicate filtering, scheduling, selectors,
CrawlSpider rules, native link candidate extraction, and Rust-owned retry, request depth, robots,
download slot, and downloader statistics policies. Native cookie jars provide isolated
Scrapy-compatible handling, while
persistent native SQLite HTTP caching supports unconditional reuse and RFC revalidation. The native
engine also owns persistent request and duplicate state, configured FIFO and LIFO crawl queues, and
start-request precedence.
The native downloader owns authenticated per-proxy connection pools. Local file and image pipelines
provide persistent freshness checks, checksums, image conversion, and thumbnails. Local, FTP, S3,
GCS, and standard-output feed exports are available with optional compression postprocessing.
SpiderOxide does not yet include remote media storage, built-in operational extensions, SOCKS proxy
support, or Scrapy command-line compatibility.

The benchmark results support further integration work, but production adoption should be based on
representative crawls that include persistence, callbacks, crawl policies, and concurrency.

## License

SpiderOxide is available under the [MIT License](LICENSE).

SpiderOxide is an independent project and is not affiliated with or endorsed by Scrapy.
