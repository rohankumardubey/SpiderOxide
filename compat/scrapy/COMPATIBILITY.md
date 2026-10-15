# Compatibility surface

The compatibility target is Scrapy 2.19. The `spideroxide-scrapy-compat` distribution maps
documented imports to SpiderOxide implementations without installing or importing upstream Scrapy.
The version-pinned [`conformance` suite](../../conformance/README.md) compares supported public
behavior with Scrapy 2.19.0 on both SpiderOxide crawl engines and publishes JSON and Markdown
reports in CI.
The [third-party certification matrix](../../certification/README.md) exercises pinned
integration components and records the exact boundaries of each result on both engines.

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
  policies; and operational extensions implemented by SpiderOxide, including AutoThrottle,
  PeriodicLog, and StatsMailer
- `scrapy.extensions.remote_control.RemoteControl`: asyncio-native, authenticated HTTP `/status`
  and explicitly opt-in `/execute` (see security notes below)
- `scrapy.extensions.telnet.TelnetConsole`: import and configuration surface only; intentionally
  does not bind a Telnet listener without Twisted reactor/Conch support (issue #48)
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
- Opt-in Twisted asyncio-reactor bridge for Deferred-returning crawler extension points, including
  downloader and spider middleware, pipelines, handlers, callbacks, and asynchronous signals

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

- Twisted Telnet/Conch interactive console and its protocol/listener — issue #48
- Non-asyncio reactors, full Twisted protocol internals, synchronous Scrapy signal-dispatch
  `Failure` results, and Deferred-returning mail — issue #50
- Third-party components beyond the tested certification boundaries

APIs not exported by the shim fail to import instead of returning placeholders. The native
asyncio replacements for some Twisted-dependent methods are described below; they are not
drop-in equivalents for Deferred-based components.

## Additive behavior

### Opt-in Twisted bridge

Install `spideroxide[twisted]` and configure
`TWISTED_REACTOR="twisted.internet.asyncioreactor.AsyncioSelectorReactor"` to accept Twisted
`Deferred` results from asynchronous extension points on either crawl engine. Both
`scrapy.utils.defer` conversion helpers and `scrapy.utils.reactor` installation and verification
helpers expose the supported asyncio-reactor subset. `Crawler(..., init_reactor=True)` also opts
in. Without opt-in, ordinary crawls do not install or start a Twisted reactor and Deferred results
fail explicitly. `scrapy-redis`'s unchanged item pipeline is certified with the opt-in setting.

The supported reactor must share the crawler's **running asyncio event loop**; an already-installed
different reactor, a reactor bound to another loop, and a mismatched `ASYNCIO_EVENT_LOOP` setting
raise errors. SpiderOxide starts Twisted's threadpool for `deferToThread` and stops it, cancels
delayed calls, and disconnects transports after the last concurrent crawler finishes. Repeated
and concurrent crawls work on the same event loop; a new event loop requires a new process
because Twisted's installed reactor remains bound to the original loop. Threadpool teardown
uses Twisted's internal cleanup hook to replace its unrestartable pool, which is covered by the
pinned Twisted integration tests. The reactor itself is not driven through Twisted's
`run()`/`stop()` lifecycle, since that would stop the asyncio loop owned by SpiderOxide. Thus
`reactor.running` remains false, reactor startup/shutdown triggers do not fire, and components
requiring them are not certified. Timers, `deferToThread`, Deferred errors, cancellation, and
timeouts are covered; browser execution and Telnet/Conch are not.

### Local control

Both control servers are **off by default**, unlike upstream Scrapy 2.19. To enable the HTTP
status server, install `spideroxide[remote-control]` and set `REMOTE_CONTROL_ENABLED=True`.
It listens only on `127.0.0.1`, on a random port, and authenticates every request with a
cryptographically generated Bearer token. A discoverable job file containing that token is
created under `$XDG_STATE_HOME/spideroxide/job_files` (or
`~/.local/state/spideroxide/job_files`), or under `REMOTE_CONTROL_JOBS_DIR` if set.
The directory must be private (mode 0700, owned by the current user) and job files have mode
0600. If this cannot be enforced, the server shuts down and no job file is left behind.
On platforms without POSIX owner/mode checks, the HTTP server is not configured.
Job files are deleted, tokens invalidated, and the listener closed on crawl shutdown.
Only grant filesystem access to trusted local users.

`POST /execute` is absent unless both `REMOTE_CONTROL_ENABLED=True` and
`REMOTE_CONTROL_EXECUTE_ENABLED=True`. Its `code` runs **arbitrary Python in the crawler
process** with access to `crawler` and a per-server `stash`; only enable this for trusted
operators. `REMOTE_CONTROL_CODE_MAX_BYTES` defaults to 64 KiB; response output and traceback
limits default to 64 KiB and 16 KiB. Async code can be cancelled at an await point according
to `REMOTE_CONTROL_TIMEOUT_DEFAULT` (30 seconds) and `REMOTE_CONTROL_TIMEOUT_MAX` (600
seconds). Like Scrapy's remote control, synchronous blocking Python code cannot be interrupted
by a timeout; neither endpoint is suitable for exposure to untrusted local users. The
TelnetConsole constructor raises `NotConfigured` even when enabled, rather than offering a
plaintext, unauthenticated substitute for Scrapy's Twisted manhole.

AutoThrottle adjusts Python-engine download slots through its public delay hook. The Rust engine
applies the same delay policy in its native slot manager; overriding AutoThrottle's `_adjust_delay`
hook in a subclass does not change the Rust policy. Twisted-backed Telnet protocol methods remain unavailable until issue #48.
StatsMailer emits its deprecation warning when enabled rather than when its shim module is
imported, since compatibility modules are registered eagerly.

Completed crawler and runner tasks resolve to SpiderOxide's `CrawlResult` instead of Scrapy's
`None`. Code that only awaits crawl completion is unchanged; callers may additionally inspect
items, statistics, and the close reason.

`MailSender.send()` schedules delivery and returns an `asyncio.Task` instead of a Twisted
`Deferred`. Await the task when the result matters; debug mode returns `None`. SMTP TLS uses
verified certificates, unlike Scrapy's permissive legacy TLS context. Twisted reactor logging
bridges and the full Deferred utility surface remain outside this subset (issue #50).

The public Scrapy-named download exceptions inherit directly from `Exception`, as in Scrapy 2.19.
SpiderOxide's additional `DownloadError` is a separate native exception; catching it alone does
not catch the Scrapy-named exceptions. The downloader and HTTP cache handle both families.

SpiderOxide's native `SignalManager.send_catch_log()` is asynchronous and returns native failure
records, whereas Scrapy's synchronous dispatcher returns Twisted `Failure` instances.
`send_catch_log_async()` supports concurrent asynchronous handlers, including Deferred results
with opt-in. Synchronous Scrapy-style signal dispatch and Twisted `Failure` results remain gaps.

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
