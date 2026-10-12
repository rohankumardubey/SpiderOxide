from __future__ import annotations

import asyncio
import gc
import logging
import math
import pickle
import sys
import time
import warnings
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from . import signals
from .compatutils import ScrapyJSONEncoder, job_dir
from .exceptions import NotConfigured, ScrapyDeprecationWarning
from .logutils import LogCounterHandler
from .trackref import live_refs

if TYPE_CHECKING:
    from .crawler import Crawler
    from .http import Request, Response
    from .python_slots import PythonDownloadSlot
    from .spider import Spider

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _numeric_stat(crawler: Crawler, name: str) -> int | float:
    value = crawler.stats.get_value(name, 0)
    if not isinstance(value, (int, float)):
        raise TypeError(f"stat {name!r} is not numeric")
    return value


async def _cancel_task(task: asyncio.Task[None] | None) -> None:
    if task is None:
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


class CoreStats:
    def __init__(self, crawler: Crawler) -> None:
        self.crawler = crawler
        self.start_time: datetime | None = None
        self._start_time_monotonic: float | None = None

    @classmethod
    def from_crawler(cls, crawler: Crawler) -> CoreStats:
        extension = cls(crawler)
        crawler.signals.connect(extension.spider_opened, signals.spider_opened)
        crawler.signals.connect(extension.spider_closed, signals.spider_closed)
        crawler.signals.connect(extension.item_scraped, signals.item_scraped)
        crawler.signals.connect(extension.item_dropped, signals.item_dropped)
        crawler.signals.connect(extension.response_received, signals.response_received)
        return extension

    def spider_opened(self, spider: Spider) -> None:
        self.start_time = _utcnow()
        self._start_time_monotonic = time.monotonic()
        self.crawler.stats.set_value("start_time", self.start_time)

    def spider_closed(self, spider: Spider, reason: str) -> None:
        finish_time = _utcnow()
        self.crawler.stats.set_value("finish_time", finish_time)
        self.crawler.stats.set_value("finish_reason", reason)
        if self._start_time_monotonic is not None:
            self.crawler.stats.set_value(
                "elapsed_time_seconds",
                time.monotonic() - self._start_time_monotonic,
            )

    def item_scraped(self, item: object, spider: Spider) -> None:
        self.crawler.stats.inc_value("item_scraped_count")

    def item_dropped(
        self,
        item: object,
        exception: BaseException,
        spider: Spider,
    ) -> None:
        self.crawler.stats.inc_value("item_dropped_count")
        self.crawler.stats.inc_value(f"item_dropped_reasons_count/{type(exception).__name__}")

    def response_received(
        self,
        spider: Spider,
    ) -> None:
        self.crawler.stats.inc_value("response_received_count")


class LogCount:
    def __init__(self, crawler: Crawler) -> None:
        self.crawler = crawler
        self.handler: LogCounterHandler | None = None

    @classmethod
    def from_crawler(cls, crawler: Crawler) -> LogCount:
        extension = cls(crawler)
        crawler.signals.connect(extension.spider_opened, signals.spider_opened)
        crawler.signals.connect(extension.spider_closed, signals.spider_closed)
        return extension

    def spider_opened(self, spider: Spider) -> None:
        self.handler = LogCounterHandler(
            self.crawler,
            level=self.crawler.settings.get("LOG_LEVEL", "DEBUG"),
        )
        logging.root.addHandler(self.handler)

    def spider_closed(self, spider: Spider, reason: str) -> None:
        if self.handler is not None:
            logging.root.removeHandler(self.handler)
            self.handler = None


class AutoThrottle:
    def __init__(self, crawler: Crawler) -> None:
        if not crawler.settings.getbool("AUTOTHROTTLE_ENABLED"):
            raise NotConfigured
        self.crawler = crawler
        self.debug = crawler.settings.getbool("AUTOTHROTTLE_DEBUG")
        self.target_concurrency = crawler.settings.getfloat("AUTOTHROTTLE_TARGET_CONCURRENCY")
        if not math.isfinite(self.target_concurrency) or self.target_concurrency <= 0:
            raise NotConfigured(
                "AUTOTHROTTLE_TARGET_CONCURRENCY "
                f"({self.target_concurrency!r}) must be higher than 0."
            )
        crawler.signals.connect(self._spider_opened, signals.spider_opened)
        crawler.signals.connect(self._response_downloaded, signals.response_downloaded)

    @classmethod
    def from_crawler(cls, crawler: Crawler) -> AutoThrottle:
        return cls(crawler)

    def _min_delay(self) -> float:
        return self.crawler.settings.getfloat("DOWNLOAD_DELAY")

    def _max_delay(self) -> float:
        return self.crawler.settings.getfloat("AUTOTHROTTLE_MAX_DELAY")

    def _start_delay(self) -> float:
        return max(self.mindelay, self.crawler.settings.getfloat("AUTOTHROTTLE_START_DELAY"))

    def _spider_opened(self, spider: Spider) -> None:
        self.mindelay = self._min_delay()
        self.maxdelay = self._max_delay()

    def _get_slot(
        self, request: Request, spider: Spider
    ) -> tuple[str | None, PythonDownloadSlot | None]:
        key = request.meta.get("download_slot")
        if not isinstance(key, str):
            return None, None
        slots = getattr(self.crawler.engine, "python_download_slots", None)
        return key, None if slots is None else slots.slots.get(key)

    def _adjust_delay(self, slot: PythonDownloadSlot, latency: float, response: Response) -> None:
        target_delay = latency / self.target_concurrency
        new_delay = max(target_delay, (slot.delay + target_delay) / 2)
        new_delay = min(max(self.mindelay, new_delay), self.maxdelay)
        if response.status != 200 and new_delay <= slot.delay:
            return
        slot.delay = new_delay

    def _response_downloaded(self, response: Response, request: Request, spider: Spider) -> None:
        key, slot = self._get_slot(request, spider)
        latency = request.meta.get("download_latency")
        if (
            latency is None
            or slot is None
            or request.meta.get("autothrottle_dont_adjust_delay") is True
        ):
            return
        old_delay = slot.delay
        self._adjust_delay(slot, float(latency), response)
        slot.next_start = max(slot.next_start, slot.last_start + slot.delay)
        self.crawler.stats.set_value(f"downloader/slot/{key}/delay", slot.delay)
        if self.debug:
            logger.info(
                "slot: %(slot)s | conc:%(concurrency)2d | delay:%(delay)5d ms "
                "(%(delaydiff)+d) | latency:%(latency)5d ms | size:%(size)6d bytes",
                {
                    "slot": key,
                    "concurrency": len(slot.transferring),
                    "delay": slot.delay * 1000,
                    "delaydiff": (slot.delay - old_delay) * 1000,
                    "latency": float(latency) * 1000,
                    "size": len(response.body),
                },
                extra={"spider": spider},
            )


class LogStats:
    def __init__(self, crawler: Crawler, interval: float) -> None:
        self.crawler = crawler
        self.interval = interval
        self._task: asyncio.Task[None] | None = None
        self._previous_pages = 0
        self._previous_items = 0

    @classmethod
    def from_crawler(cls, crawler: Crawler) -> LogStats:
        interval = crawler.settings.getfloat("LOGSTATS_INTERVAL", 60.0)
        if interval <= 0:
            raise NotConfigured
        extension = cls(crawler, interval)
        crawler.signals.connect(extension.spider_opened, signals.spider_opened)
        crawler.signals.connect(extension.spider_closed, signals.spider_closed)
        return extension

    def spider_opened(self, spider: Spider) -> None:
        self._log(spider)
        self._task = asyncio.create_task(self._periodic_log(spider))

    async def spider_closed(self, spider: Spider, reason: str) -> None:
        await _cancel_task(self._task)
        start_time = self.crawler.stats.get_value("start_time")
        finish_time = self.crawler.stats.get_value("finish_time")
        if not isinstance(start_time, datetime) or not isinstance(finish_time, datetime):
            return
        minutes = (finish_time - start_time).total_seconds() / 60
        pages = _numeric_stat(self.crawler, "response_received_count")
        items = _numeric_stat(self.crawler, "item_scraped_count")
        self.crawler.stats.set_value(
            "responses_per_minute",
            pages / minutes if minutes else None,
        )
        self.crawler.stats.set_value(
            "items_per_minute",
            items / minutes if minutes else None,
        )

    async def _periodic_log(self, spider: Spider) -> None:
        while True:
            await asyncio.sleep(self.interval)
            self._log(spider)

    def _log(self, spider: Spider) -> None:
        pages = int(_numeric_stat(self.crawler, "response_received_count"))
        items = int(_numeric_stat(self.crawler, "item_scraped_count"))
        page_rate = round((pages - self._previous_pages) * 60 / self.interval)
        item_rate = round((items - self._previous_items) * 60 / self.interval)
        self._previous_pages = pages
        self._previous_items = items
        logger.info(
            "Crawled %d pages (at %d pages/min), scraped %d items (at %d items/min)",
            pages,
            page_rate,
            items,
            item_rate,
            extra={"spider": spider},
        )


class CloseSpider:
    def __init__(self, crawler: Crawler) -> None:
        self.crawler = crawler
        settings = crawler.settings
        self.timeout = settings.getfloat("CLOSESPIDER_TIMEOUT", 0.0)
        self.item_count = settings.getint("CLOSESPIDER_ITEMCOUNT", 0)
        self.page_count = settings.getint("CLOSESPIDER_PAGECOUNT", 0)
        self.error_count = settings.getint("CLOSESPIDER_ERRORCOUNT", 0)
        self.timeout_no_item = settings.getint("CLOSESPIDER_TIMEOUT_NO_ITEM", 0)
        self.page_count_no_item = settings.getint("CLOSESPIDER_PAGECOUNT_NO_ITEM", 0)
        values = (
            self.timeout,
            self.item_count,
            self.page_count,
            self.error_count,
            self.timeout_no_item,
            self.page_count_no_item,
        )
        if any(value < 0 for value in values):
            raise ValueError("CLOSESPIDER settings cannot be negative")
        if not any(values):
            raise NotConfigured
        self._items = 0
        self._pages = 0
        self._errors = 0
        self._pages_without_items = 0
        self._items_in_period = 0
        self._timeout_task: asyncio.Task[None] | None = None
        self._no_item_task: asyncio.Task[None] | None = None

    @classmethod
    def from_crawler(cls, crawler: Crawler) -> CloseSpider:
        extension = cls(crawler)
        crawler.signals.connect(extension.spider_opened, signals.spider_opened)
        crawler.signals.connect(extension.spider_closed, signals.spider_closed)
        crawler.signals.connect(extension.item_scraped, signals.item_scraped)
        crawler.signals.connect(extension.response_received, signals.response_received)
        crawler.signals.connect(extension.spider_error, signals.spider_error)
        return extension

    def spider_opened(self, spider: Spider) -> None:
        if self.timeout:
            self._timeout_task = asyncio.create_task(
                self._close_after(self.timeout, "closespider_timeout")
            )
        if self.timeout_no_item:
            self._no_item_task = asyncio.create_task(self._check_items_periodically())

    async def spider_closed(self, spider: Spider, reason: str) -> None:
        await _cancel_task(self._timeout_task)
        await _cancel_task(self._no_item_task)

    def item_scraped(self, item: object, spider: Spider) -> None:
        self._items += 1
        self._items_in_period += 1
        self._pages_without_items = 0
        if self.item_count and self._items == self.item_count:
            self._close("closespider_itemcount")

    def response_received(
        self,
        response: Response,
        request: Request,
        spider: Spider,
    ) -> None:
        self._pages += 1
        self._pages_without_items += 1
        if self.page_count and self._pages == self.page_count:
            self._close("closespider_pagecount")
            return
        if self.page_count_no_item and self._pages_without_items >= self.page_count_no_item:
            self._close("closespider_pagecount_no_item")

    def spider_error(
        self,
        failure: BaseException,
        request: Request,
        spider: Spider,
        response: Response | None = None,
    ) -> None:
        self._errors += 1
        if self.error_count and self._errors == self.error_count:
            self._close("closespider_errorcount")

    async def _close_after(self, delay: float, reason: str) -> None:
        await asyncio.sleep(delay)
        self._close(reason)

    async def _check_items_periodically(self) -> None:
        while True:
            await asyncio.sleep(self.timeout_no_item)
            if self._items_in_period == 0:
                self._close("closespider_timeout_no_item")
                return
            self._items_in_period = 0

    def _close(self, reason: str) -> None:
        engine = self.crawler.engine
        if engine is None:
            raise RuntimeError("crawl engine is not initialized")
        engine.close_spider(reason)


def _process_memory_bytes() -> int:
    try:
        import resource
    except ImportError as error:
        raise NotConfigured("memory usage monitoring is unavailable on this platform") from error
    usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return int(usage if sys.platform == "darwin" else usage * 1024)


class MemoryUsage:
    def __init__(self, crawler: Crawler) -> None:
        if not crawler.settings.getbool("MEMUSAGE_ENABLED", True):
            raise NotConfigured
        try:
            import resource  # noqa: F401
        except ImportError as error:
            raise NotConfigured("memory usage monitoring is unavailable") from error
        self.crawler = crawler
        settings = crawler.settings
        self.limit = settings.getint("MEMUSAGE_LIMIT_MB", 0) * 1024 * 1024
        self.warning = settings.getint("MEMUSAGE_WARNING_MB", 0) * 1024 * 1024
        self.interval = settings.getfloat("MEMUSAGE_CHECK_INTERVAL_SECONDS", 60.0)
        if min(self.limit, self.warning) < 0:
            raise ValueError("MEMUSAGE limits cannot be negative")
        if self.interval <= 0:
            raise ValueError("MEMUSAGE_CHECK_INTERVAL_SECONDS must be greater than zero")
        self._warning_reported = False
        self._task: asyncio.Task[None] | None = None

    @classmethod
    def from_crawler(cls, crawler: Crawler) -> MemoryUsage:
        extension = cls(crawler)
        crawler.signals.connect(extension.engine_started, signals.engine_started)
        crawler.signals.connect(extension.engine_stopped, signals.engine_stopped)
        return extension

    def engine_started(self) -> None:
        startup = _process_memory_bytes()
        self.crawler.stats.set_value("memusage/startup", startup)
        self.crawler.stats.set_value("memusage/max", startup)
        self._task = asyncio.create_task(self._periodic_check())

    async def engine_stopped(self) -> None:
        await _cancel_task(self._task)

    async def _periodic_check(self) -> None:
        while True:
            await self._update()
            await asyncio.sleep(self.interval)

    async def _update(self) -> None:
        current = _process_memory_bytes()
        self.crawler.stats.max_value("memusage/max", current)
        spider = self.crawler.spider
        if self.limit and current > self.limit:
            self.crawler.stats.set_value("memusage/limit_reached", 1)
            if spider is not None:
                spider.logger.error(
                    "Memory usage exceeded %d MiB; shutting down",
                    self.limit // (1024 * 1024),
                )
            engine = self.crawler.engine
            if engine is not None:
                engine.close_spider("memusage_exceeded")
        if self.warning and current > self.warning and not self._warning_reported:
            self.crawler.stats.set_value("memusage/warning_reached", 1)
            if spider is not None:
                spider.logger.warning(
                    "Memory usage exceeded %d MiB",
                    self.warning // (1024 * 1024),
                )
            await self.crawler.signals.send(signals.memusage_warning_reached)
            self._warning_reported = True


class MemoryDebugger:
    def __init__(self, crawler: Crawler) -> None:
        if not crawler.settings.getbool("MEMDEBUG_ENABLED", False):
            raise NotConfigured
        self.crawler = crawler

    @classmethod
    def from_crawler(cls, crawler: Crawler) -> MemoryDebugger:
        extension = cls(crawler)
        crawler.signals.connect(extension.spider_closed, signals.spider_closed)
        return extension

    def spider_closed(self, spider: Spider, reason: str) -> None:
        gc.collect()
        self.crawler.stats.set_value("memdebug/gc_garbage_count", len(gc.garbage))
        for reference_type, references in live_refs.items():
            if references:
                self.crawler.stats.set_value(
                    f"memdebug/live_refs/{reference_type.__name__}",
                    len(references),
                )


class SpiderState:
    def __init__(self, jobdir: str | None = None) -> None:
        self.jobdir = jobdir

    @classmethod
    def from_crawler(cls, crawler: Crawler) -> SpiderState:
        directory = job_dir(crawler.settings)
        if directory is None:
            raise NotConfigured
        extension = cls(directory)
        extension.crawler = crawler
        crawler.signals.connect(extension.spider_opened, signals.spider_opened)
        crawler.signals.connect(extension.spider_closed, signals.spider_closed)
        return extension

    @property
    def statefn(self) -> str:
        if self.jobdir is None:
            raise RuntimeError("SpiderState requires a job directory")
        return str(Path(self.jobdir, "spider.state"))

    def spider_opened(self, spider: Spider) -> None:
        engine = getattr(getattr(self, "crawler", None), "engine", None)
        if getattr(engine, "backend_name", None) == "rust":
            return
        state_path = Path(self.statefn)
        if state_path.exists():
            with state_path.open("rb") as state_file:
                spider.state = pickle.load(state_file)
        else:
            spider.state = {}

    def spider_closed(self, spider: Spider) -> None:
        engine = getattr(getattr(self, "crawler", None), "engine", None)
        if getattr(engine, "backend_name", None) == "rust":
            return
        with Path(self.statefn).open("wb") as state_file:
            pickle.dump(spider.state, state_file, protocol=4)


def _periodic_filter(value: object, setting_name: str) -> dict[str, tuple[str, ...]] | None:
    if value is False or value is None:
        return None
    if value is True:
        return {"include": (), "exclude": ()}
    if not isinstance(value, Mapping):
        raise TypeError(f"{setting_name} must be a boolean or mapping")
    if not value:
        return None
    parsed: dict[str, tuple[str, ...]] = {}
    for name in ("include", "exclude"):
        raw_patterns = value.get(name, ())
        if isinstance(raw_patterns, str):
            patterns = (raw_patterns,)
        else:
            try:
                patterns = tuple(raw_patterns)
            except TypeError as error:
                raise TypeError(
                    f"{setting_name}.{name} must be a string or iterable of strings"
                ) from error
        if not all(isinstance(pattern, str) for pattern in patterns):
            raise TypeError(f"{setting_name}.{name} must contain only strings")
        parsed[name] = patterns
    return parsed


class PeriodicLog:
    def __init__(
        self,
        stats: object,
        interval: float = 60.0,
        ext_stats: dict[str, object] | None = None,
        ext_delta: dict[str, object] | None = None,
        ext_timing_enabled: bool = False,
    ) -> None:
        self.stats = stats
        self.interval = interval
        self._multiplier = 60.0 / interval
        self.encoder = ScrapyJSONEncoder(sort_keys=True, indent=4)
        self.ext_stats_enabled = bool(ext_stats)
        self.ext_delta_enabled = bool(ext_delta)
        self.ext_stats_include = tuple(ext_stats.get("include", ())) if ext_stats else ()
        self.ext_stats_exclude = tuple(ext_stats.get("exclude", ())) if ext_stats else ()
        self.ext_delta_include = tuple(ext_delta.get("include", ())) if ext_delta else ()
        self.ext_delta_exclude = tuple(ext_delta.get("exclude", ())) if ext_delta else ()
        self.ext_timing_enabled = ext_timing_enabled
        self.task: asyncio.Task[None] | None = None
        self._task: asyncio.Task[None] | None = None

    @property
    def multiplier(self) -> float:
        warnings.warn(
            "The PeriodicLog.multiplier attribute is deprecated.",
            ScrapyDeprecationWarning,
            stacklevel=2,
        )
        return self._multiplier

    @multiplier.setter
    def multiplier(self, value: float) -> None:
        warnings.warn(
            "The PeriodicLog.multiplier attribute is deprecated.",
            ScrapyDeprecationWarning,
            stacklevel=2,
        )
        self._multiplier = value

    @classmethod
    def from_crawler(cls, crawler: Crawler) -> PeriodicLog:
        settings = crawler.settings
        interval = settings.getfloat("LOGSTATS_INTERVAL", 60.0)
        stats_config = _periodic_filter(settings.get("PERIODIC_LOG_STATS"), "PERIODIC_LOG_STATS")
        delta_config = _periodic_filter(settings.get("PERIODIC_LOG_DELTA"), "PERIODIC_LOG_DELTA")
        timing_enabled = settings.getbool("PERIODIC_LOG_TIMING_ENABLED", False)
        if interval <= 0 or (stats_config is None and delta_config is None and not timing_enabled):
            raise NotConfigured
        extension = cls(crawler.stats, interval, stats_config, delta_config, timing_enabled)
        crawler.signals.connect(extension.spider_opened, signals.spider_opened)
        crawler.signals.connect(extension.spider_closed, signals.spider_closed)
        return extension

    def spider_opened(self, spider: Spider) -> None:
        self.time_prev = _utcnow()
        self.delta_prev: dict[str, int | float] = {}
        self.stats_prev: dict[str, object] = {}
        self.log()
        self.task = self._task = asyncio.create_task(self._periodic_log())

    async def spider_closed(self, spider: Spider, reason: str) -> None:
        await _cancel_task(self._task)
        self.task = self._task = None
        self.log()

    async def _periodic_log(self) -> None:
        while True:
            await asyncio.sleep(self.interval)
            self.log()

    def log(self) -> None:
        payload: dict[str, object] = {}
        if self.ext_timing_enabled:
            payload.update(self.log_timing())
        if self.ext_delta_enabled:
            payload.update(self.log_delta())
        if self.ext_stats_enabled:
            payload.update(self.log_crawler_stats())
        logger.info(self.encoder.encode(payload))

    def param_allowed(
        self, stat_name: str, include: tuple[str, ...], exclude: tuple[str, ...]
    ) -> bool:
        return (not include or any(pattern in stat_name for pattern in include)) and not any(
            pattern in stat_name for pattern in exclude
        )

    def log_delta(self) -> dict[str, object]:
        current = {
            key: value
            for key, value in self.stats.get_stats().items()
            if isinstance(value, (int, float))
            and self.param_allowed(key, self.ext_delta_include, self.ext_delta_exclude)
        }
        delta = {key: value - self.delta_prev.get(key, 0) for key, value in current.items()}
        self.delta_prev = current
        return {"delta": delta}

    def log_crawler_stats(self) -> dict[str, object]:
        return {
            "stats": {
                key: value
                for key, value in self.stats.get_stats().items()
                if self.param_allowed(key, self.ext_stats_include, self.ext_stats_exclude)
            }
        }

    def log_timing(self) -> dict[str, object]:
        now = _utcnow()
        start_time = self.stats.get_value("start_time")
        if not isinstance(start_time, datetime):
            raise TypeError("start_time stat must be a datetime")
        values = {
            "log_interval": self.interval,
            "start_time": start_time,
            "utcnow": now,
            "log_interval_real": (now - self.time_prev).total_seconds(),
            "elapsed": (now - start_time).total_seconds(),
        }
        self.time_prev = now
        return {"time": values}


__all__ = [
    "AutoThrottle",
    "CloseSpider",
    "CoreStats",
    "LogCount",
    "LogStats",
    "MemoryDebugger",
    "MemoryUsage",
    "PeriodicLog",
    "SpiderState",
]
