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
- The `scrapy` executable; project and spider generation; crawl, runspider, fetch, view, parse,
  shell, list, edit, settings, version, check, and benchmark command surfaces
- Spider contract discovery, built-in and custom contracts, synchronous and asynchronous callback
  validation, and compatible `scrapy check` reporting and exit codes
- `SitemapSpider`, `XMLFeedSpider`, `CSVFeedSpider`, and sitemap and feed iterator utilities
- Asyncio-native crawler runners, standalone crawler processes, spider loading, graceful
  shutdown, task tracking, and runtime component lookup
- Built-in downloader and spider middleware modules and their Scrapy 2.19 class paths
- File and image pipelines; Python, pretty-print, JSON, JSON Lines, CSV, XML, Marshal, and Pickle
  item exporters; filesystem, stdout, FTP, FTPS, S3, and GCS feed storage; post-processing; cache
  policies; and operational extensions implemented by SpiderOxide
- HTTP, file, data URI, FTP, and S3 download-handler class paths
- Scheduler queue names, duplicate filters, request fingerprints, request serialization helpers,
  response dictionary round-tripping, spider discovery and selection, project settings loading,
  object loading, raw HTTP request formatting, response status messages, base URL and meta-refresh
  parsing, browser-file generation, and common encoding and URL helpers
- Scrapy's public exception inheritance and attributes; deprecated exception aliases and warnings
- Sender-aware, weak-receiver, and asynchronous signal dispatch; asyncio signal waiting and bulk
  disconnect; asyncio-native task scheduling, generator, looping, sleep, and thread helpers
- LogFormatter customization and log-record helpers; SMTP mail with deprecation warnings;
  cURL conversion, JSON serialization, job-directory, URL, and live-reference debugging helpers;
  documented Python, misc, deprecation, and project utility functions
- One-time import of standard Scrapy 2.19 `JOBDIR` layouts into SpiderOxide's native persistence
  format, including queued requests, duplicate fingerprints, callbacks, priorities, and spider state

## Persistence and project formats

SpiderOxide reads and writes its versioned SQLite `JOBDIR` format. Native schemas 1 and 2 migrate to
schema 3. When a job directory contains only Scrapy 2.19 state, SpiderOxide can atomically import the
standard `ScrapyPriorityQueue` layout using Pickle or Marshal FIFO/LIFO disk queues, the default
20-byte request fingerprints, protocol-4 spider state, and built-in request classes. The original
Scrapy files remain byte-for-byte unchanged and become a downgrade snapshot; SpiderOxide does not
export subsequent native progress back into them.

Custom priority queues, custom request classes, nonstandard fingerprints, corrupt files, and
directories containing both unmarked native and Scrapy state are rejected without replacing or
deleting source data. Pickle-based job directories are trusted-code inputs and must not be imported
from untrusted sources.

Scrapy project settings modules, generated project and spider layouts, feed configuration, command
discovery, and `scrapy.cfg` project-root discovery are supported directly. They do not require a
format migration.

## Public roadmap gaps

The shim intentionally omits public APIs that SpiderOxide has not implemented yet:

- Telnet and remote-control surfaces — issue #48
- Twisted Deferred and reactor integration, including synchronous Scrapy signal-dispatch
  results, `Failure` objects, and Deferred-returning mail — issue #50
- Certification against third-party extensions and their additional utility needs — issue #49

APIs not exported by the shim fail to import instead of returning placeholders. The native
asyncio replacements for some Twisted-dependent methods are described below; they are not
drop-in equivalents for Deferred-based components.

## Additive behavior

Completed crawler and runner tasks resolve to SpiderOxide's `CrawlResult` instead of Scrapy's
`None`. Code that only awaits crawl completion is unchanged; callers may additionally inspect
items, statistics, and the close reason.

`MailSender.send()` schedules delivery and returns an `asyncio.Task` instead of a Twisted
`Deferred`. Await the task when the result matters; debug mode returns `None`. SMTP TLS uses
verified certificates, unlike Scrapy's permissive legacy TLS context. Twisted reactor logging
bridges and Deferred-specific helpers remain outside this surface (issue #50).

The public Scrapy-named download exceptions inherit directly from `Exception`, as in Scrapy 2.19.
SpiderOxide's additional `DownloadError` is a separate native exception; catching it alone does
not catch the Scrapy-named exceptions. The downloader and HTTP cache handle both families.

SpiderOxide's native `SignalManager.send_catch_log()` is asynchronous and returns native failure
records, whereas Scrapy's synchronous dispatcher returns Twisted `Failure` instances.
`send_catch_log_async()` supports concurrent asynchronous handlers; Twisted-style signal dispatch
and Deferred results are tracked under issue #50.

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
