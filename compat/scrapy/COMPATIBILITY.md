# Compatibility surface

The compatibility target is Scrapy 2.19. The `spideroxide-scrapy-compat` distribution maps
documented imports to SpiderOxide implementations without installing or importing upstream Scrapy.
The version-pinned [`conformance` suite](../../conformance/README.md) compares supported public
behavior with Scrapy 2.19.0 on both SpiderOxide crawl engines and publishes JSON and Markdown
reports in CI.

## Supported

- Top-level `Spider`, `Request`, `FormRequest`, `Item`, `Field`, and `Selector` imports
- Request, response, header, item, selector, loader, link, crawl-rule, settings, signal, and
  statistics modules
- Scrapy settings priorities, module and environment loading, mutation and freezing, deep copies,
  component ordering, disabling, replacement, and `from_crawler` construction
- Scrapy add-on ordering, pre-crawler and crawler settings hooks, opt-outs, and runtime lookup
- `SitemapSpider`, `XMLFeedSpider`, `CSVFeedSpider`, and sitemap and feed iterator utilities
- Asyncio-native crawler runners, standalone crawler processes, spider loading, graceful
  shutdown, task tracking, and runtime component lookup
- Built-in downloader and spider middleware modules and their Scrapy 2.19 class paths
- File and image pipelines, item exporters, feed storage, post-processing, cache policies, and
  operational extensions implemented by SpiderOxide
- HTTP, file, data URI, FTP, and S3 download-handler class paths
- Scheduler queue names, duplicate filters, request fingerprints, request serialization helpers,
  project settings loading, object loading, and common encoding and URL helpers

## Public roadmap gaps

The shim intentionally omits public APIs that SpiderOxide has not implemented yet:

- Additional exporters and feed-storage classes — issue #44
- Spider contracts — issue #45
- Remaining logging, mail, resolver, TLS, and utility APIs — issues #46 and #47
- Telnet and remote-control surfaces — issue #48
- Deferred and reactor integration — issue #50
- CLI commands and the `scrapy` executable — issue #40

Importing one of these APIs fails normally instead of returning a placeholder with incorrect
behavior.

## Additive behavior

Completed crawler and runner tasks resolve to SpiderOxide's `CrawlResult` instead of Scrapy's
`None`. Code that only awaits crawl completion is unchanged; callers may additionally inspect
items, statistics, and the close reason.

SpiderOxide additionally exposes `scrapy.services.ServiceManager`, the `SERVICES` setting, and
`Crawler.get_service()` for dependency-ordered crawler services with synchronous or asynchronous
startup and shutdown hooks. Scrapy 2.19 has no corresponding runtime-service API.

## Unsupported private APIs

SpiderOxide does not claim compatibility for undocumented or private Scrapy implementation details,
including:

- Names beginning with `_` in any Scrapy module
- `scrapy.core.engine.ExecutionEngine`, `scrapy.core.scraper.Scraper`, and downloader slot internals
- Twisted reactor, Deferred, DNS, TLS, and protocol implementation internals
- Scrapy test helpers, benchmark servers, test-process helpers, and internal command helpers
- Private request fingerprint caches, weak-reference bookkeeping, and middleware adapter internals

Projects that depend on these surfaces must continue using upstream Scrapy until the dependency is
removed or an explicit compatibility API is added.
