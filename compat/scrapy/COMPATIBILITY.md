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
  project settings loading, object loading, and common encoding and URL helpers
- Asyncio signal waiting, bulk disconnect, and asynchronous signal dispatch
- LogFormatter customization and log-record helpers; SMTP mail with deprecation warnings;
  cURL conversion, JSON serialization, job-directory, URL, and live-reference debugging helpers
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

- Remaining logging, signal, Deferred, and miscellaneous utility APIs — issue #47
- Telnet and remote-control surfaces — issue #48
- Deferred and reactor integration — issue #50

Importing one of these APIs fails normally instead of returning a placeholder with incorrect
behavior.

## Additive behavior

Completed crawler and runner tasks resolve to SpiderOxide's `CrawlResult` instead of Scrapy's
`None`. Code that only awaits crawl completion is unchanged; callers may additionally inspect
items, statistics, and the close reason.

`MailSender.send()` schedules delivery and returns an `asyncio.Task` instead of a Twisted
`Deferred`. Await the task when the result matters; debug mode returns `None`. SMTP TLS uses
verified certificates, unlike Scrapy's permissive legacy TLS context. Twisted reactor logging
bridges and Deferred-specific helpers remain outside this surface (issue #50).

Native SpiderOxide download exceptions retain the additional `DownloadError`/`SpiderOxideError`
base classes, so their full method-resolution order does not yet match Scrapy's directly inherited
exceptions. The Scrapy exception names, error meanings, and tested attributes are available;
code depending on exact base-class identity must remain on upstream Scrapy (issue #47).

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
