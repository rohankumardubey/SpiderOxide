from __future__ import annotations

import asyncio
import json
import logging
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from spideroxide import (  # noqa: E402
    CloseSpiderExtension,
    CoreStats,
    Crawler,
    DropItem,
    Field,
    Item,
    LogCount,
    LogStats,
    MemoryDebugger,
    MemoryUsage,
    PeriodicLog,
    Request,
    Response,
    SignalManager,
    Spider,
    SpiderState,
    TextResponse,
    signals,
)


class OperationalSpider(Spider):
    name = "operational"

    def __init__(self, count: int = 5, *, fail: bool = False, items: bool = True) -> None:
        super().__init__()
        self.count = count
        self.fail = fail
        self.items = items

    def start_requests(self) -> object:
        for index in range(self.count):
            yield Request(f"data:text/plain,{index}", dont_filter=True)

    def parse(self, response: TextResponse) -> object:
        if self.fail:
            raise RuntimeError("expected operational failure")
        if self.items:
            return {"value": response.text}
        return None


class SlowHandler:
    lazy = True

    async def download_request(self, request: Request) -> Response:
        await asyncio.sleep(10)
        return Response(request.url)


class ShortDelayHandler:
    lazy = True

    async def download_request(self, request: Request) -> Response:
        await asyncio.sleep(0.01)
        return Response(request.url)


class SlowSpider(Spider):
    name = "slow-operational"
    start_urls = ["slow://example.test/resource"]

    def parse(self, response: Response) -> None:
        return None


class DropPipeline:
    def process_item(self, item: object, spider: Spider) -> object:
        raise DropItem("expected operational drop")


class TrackedItem(Item):
    value = Field()


class StateSpider(Spider):
    name = "state-operational"
    start_urls = ["data:text/plain,state"]

    def parse(self, response: TextResponse) -> None:
        self.state["runs"] = self.state.get("runs", 0) + 1


class LoggingSpider(Spider):
    name = "logging-operational"
    start_urls = ["data:text/plain,logging"]

    def parse(self, response: TextResponse) -> None:
        self.logger.debug("debug message should be filtered")
        self.logger.error("error message should be counted")


class FailingCloseExtension:
    @classmethod
    def from_crawler(cls, crawler: Crawler) -> FailingCloseExtension:
        extension = cls()
        crawler.signals.connect(extension.spider_closed, signals.spider_closed)
        return extension

    def spider_closed(self, spider: Spider, reason: str) -> None:
        raise RuntimeError("expected close-signal failure")


class RecordHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


async def _verify_core_stats(engine: str) -> None:
    crawler = Crawler(
        OperationalSpider,
        {
            "ENGINE_BACKEND": engine,
            "LOGSTATS_INTERVAL": 0,
        },
    )
    result = await crawler.crawl(2)
    assert result.items == ({"value": "0"}, {"value": "1"})
    assert result.stats["response_received_count"] == 2
    assert result.stats["item_scraped_count"] == 2
    assert result.stats["finish_reason"] == "finished"
    assert isinstance(result.stats["start_time"], datetime)
    assert isinstance(result.stats["finish_time"], datetime)
    assert result.stats["elapsed_time_seconds"] >= 0
    assert isinstance(crawler.extensions.get_by_type(CoreStats), CoreStats)
    assert crawler.extensions.get_by_type(LogStats) is None

    rated = await Crawler(
        OperationalSpider,
        {
            "ENGINE_BACKEND": engine,
            "LOGSTATS_INTERVAL": 60,
        },
    ).crawl(1)
    assert rated.stats["responses_per_minute"] >= 0
    assert rated.stats["items_per_minute"] >= 0

    failed = await Crawler(
        OperationalSpider,
        {
            "ENGINE_BACKEND": engine,
            "LOGSTATS_INTERVAL": 0,
        },
    ).crawl(1, fail=True)
    assert failed.stats["spider_exceptions/count"] == 1
    assert failed.stats["spider_exceptions/builtins.RuntimeError"] == 1

    dropped = await Crawler(
        OperationalSpider,
        {
            "ENGINE_BACKEND": engine,
            "ITEM_PIPELINES": [DropPipeline],
            "LOGSTATS_INTERVAL": 0,
        },
    ).crawl(1)
    assert dropped.stats["item_dropped_count"] == 1
    assert dropped.stats["item_dropped_reasons_count/DropItem"] == 1


async def _verify_signal_argument_filtering() -> None:
    manager = SignalManager()
    seen: list[Spider] = []

    def failing_receiver(spider: Spider) -> None:
        raise RuntimeError("expected signal failure")

    def reduced_receiver(spider: Spider) -> None:
        seen.append(spider)

    spider = OperationalSpider()
    manager.connect(failing_receiver, signals.response_received)
    manager.connect(reduced_receiver, signals.response_received)
    with patch("spideroxide.signals.logger.exception"):
        responses = await manager.send(
            signals.response_received,
            response=Response("https://example.test"),
            request=Request("https://example.test"),
            spider=spider,
        )
    assert seen == [spider]
    assert isinstance(responses[0][1], signals.SignalFailure)
    assert isinstance(responses[0][1].exception, RuntimeError)


async def _verify_log_count(engine: str) -> None:
    crawler = Crawler(
        LoggingSpider,
        {
            "ENGINE_BACKEND": engine,
            "EXTENSIONS": {FailingCloseExtension: -100},
            "LOG_LEVEL": "WARNING",
            "LOGSTATS_INTERVAL": 0,
        },
    )
    with patch("spideroxide.signals.logger.exception"):
        result = await crawler.crawl()
    extension = crawler.extensions.get_by_type(LogCount)
    assert isinstance(extension, LogCount)
    assert result.stats["log_count/ERROR"] == 1
    assert "log_count/DEBUG" not in result.stats
    assert extension.handler is None


async def _verify_close_spider(engine: str) -> None:
    cases = (
        ({"CLOSESPIDER_ITEMCOUNT": 1}, {"items": True}, "closespider_itemcount"),
        ({"CLOSESPIDER_PAGECOUNT": 2}, {"items": False}, "closespider_pagecount"),
        ({"CLOSESPIDER_ERRORCOUNT": 2}, {"fail": True}, "closespider_errorcount"),
        (
            {"CLOSESPIDER_PAGECOUNT_NO_ITEM": 2},
            {"items": False},
            "closespider_pagecount_no_item",
        ),
    )
    for settings, spider_options, reason in cases:
        crawler = Crawler(
            OperationalSpider,
            {
                "ENGINE_BACKEND": engine,
                "CONCURRENT_REQUESTS": 1,
                "LOGSTATS_INTERVAL": 0,
                **settings,
            },
        )
        result = await crawler.crawl(10, **spider_options)
        assert result.reason == reason
        assert isinstance(
            crawler.extensions.get_by_type(CloseSpiderExtension), CloseSpiderExtension
        )

    precedence = await Crawler(
        OperationalSpider,
        {
            "ENGINE_BACKEND": engine,
            "CONCURRENT_REQUESTS": 1,
            "LOGSTATS_INTERVAL": 0,
            "CLOSESPIDER_PAGECOUNT": 2,
            "CLOSESPIDER_PAGECOUNT_NO_ITEM": 2,
        },
    ).crawl(10, items=False)
    assert precedence.reason == "closespider_pagecount"

    for setting, reason in (("CLOSESPIDER_TIMEOUT", "closespider_timeout"),):
        result = await Crawler(
            SlowSpider,
            {
                "ENGINE_BACKEND": engine,
                "CONCURRENT_REQUESTS": 1,
                "DOWNLOAD_HANDLERS": {"slow": SlowHandler},
                "LOGSTATS_INTERVAL": 0,
                setting: 0.01,
            },
        ).crawl()
        assert result.reason == reason

    no_item_result = await Crawler(
        SlowSpider,
        {
            "ENGINE_BACKEND": engine,
            "CONCURRENT_REQUESTS": 1,
            "DOWNLOAD_HANDLERS": {"slow": SlowHandler},
            "LOGSTATS_INTERVAL": 0,
            "CLOSESPIDER_TIMEOUT_NO_ITEM": 1,
        },
    ).crawl()
    assert no_item_result.reason == "closespider_timeout_no_item"


async def _verify_periodic_logging() -> None:
    log_handler = RecordHandler()
    operational_logger = logging.getLogger("spideroxide.operational")
    operational_logger.addHandler(log_handler)
    operational_logger.setLevel(logging.INFO)
    try:
        result = await Crawler(
            SlowSpider,
            {
                "DOWNLOAD_HANDLERS": {"slow": ShortDelayHandler},
                "LOGSTATS_INTERVAL": 0.005,
                "PERIODIC_LOG_STATS": True,
                "PERIODIC_LOG_DELTA": {
                    "include": ["response", "item"],
                    "exclude": ["item"],
                },
                "PERIODIC_LOG_TIMING_ENABLED": True,
                "EXTENSIONS": {"spideroxide.operational.PeriodicLog": 0},
            },
        ).crawl()
    finally:
        operational_logger.removeHandler(log_handler)
    assert result.reason == "finished"
    assert any(message.startswith("Crawled ") for message in log_handler.messages)
    periodic_messages = [message for message in log_handler.messages if message.startswith("{")]
    assert len(periodic_messages) >= 2
    final_payload = json.loads(periodic_messages[-1])
    assert "response_received_count" in final_payload["delta"]
    assert "item_scraped_count" not in final_payload["delta"]
    assert set(final_payload["time"]) == {
        "elapsed",
        "log_interval",
        "log_interval_real",
        "start_time",
        "utcnow",
    }


async def _verify_memory_extensions(engine: str) -> None:
    with patch("spideroxide.operational._process_memory_bytes", return_value=2 * 1024 * 1024):
        crawler = Crawler(
            SlowSpider,
            {
                "ENGINE_BACKEND": engine,
                "DOWNLOAD_HANDLERS": {"slow": SlowHandler},
                "LOGSTATS_INTERVAL": 0,
                "MEMUSAGE_ENABLED": True,
                "MEMUSAGE_LIMIT_MB": 1,
                "MEMUSAGE_CHECK_INTERVAL_SECONDS": 0.005,
                "MEMDEBUG_ENABLED": True,
            },
        )
        result = await crawler.crawl()
    assert result.reason == "memusage_exceeded"
    assert result.stats["memusage/startup"] == 2 * 1024 * 1024
    assert result.stats["memusage/max"] == 2 * 1024 * 1024
    assert result.stats["memusage/limit_reached"] == 1
    assert "memdebug/gc_garbage_count" in result.stats
    assert isinstance(crawler.extensions.get_by_type(MemoryUsage), MemoryUsage)
    assert isinstance(crawler.extensions.get_by_type(MemoryDebugger), MemoryDebugger)
    assert crawler.extensions.get_by_type(PeriodicLog) is None

    tracked_item = TrackedItem(value="retained")
    with patch("spideroxide.operational._process_memory_bytes", return_value=1024 * 1024):
        debug_result = await Crawler(
            OperationalSpider,
            {
                "ENGINE_BACKEND": engine,
                "LOGSTATS_INTERVAL": 0,
                "MEMDEBUG_ENABLED": True,
            },
        ).crawl(0)
    assert tracked_item["value"] == "retained"
    assert debug_result.stats["memdebug/live_refs/TrackedItem"] >= 1

    warnings = 0

    async def warning_receiver() -> None:
        nonlocal warnings
        warnings += 1

    with patch("spideroxide.operational._process_memory_bytes", return_value=2 * 1024 * 1024):
        warning_crawler = Crawler(
            SlowSpider,
            {
                "ENGINE_BACKEND": engine,
                "DOWNLOAD_HANDLERS": {"slow": SlowHandler},
                "LOGSTATS_INTERVAL": 0,
                "CLOSESPIDER_TIMEOUT": 0.015,
                "MEMUSAGE_WARNING_MB": 1,
                "MEMUSAGE_CHECK_INTERVAL_SECONDS": 0.005,
            },
        )
        warning_crawler.signals.connect(
            warning_receiver,
            signals.memusage_warning_reached,
        )
        warning_result = await warning_crawler.crawl()
    assert warning_result.reason == "closespider_timeout"
    assert warning_result.stats["memusage/warning_reached"] == 1
    assert warnings == 1

    with patch("spideroxide.operational._process_memory_bytes", return_value=1024 * 1024):
        strict_result = await Crawler(
            SlowSpider,
            {
                "ENGINE_BACKEND": engine,
                "DOWNLOAD_HANDLERS": {"slow": SlowHandler},
                "LOGSTATS_INTERVAL": 0,
                "CLOSESPIDER_TIMEOUT": 0.015,
                "MEMUSAGE_LIMIT_MB": 1,
                "MEMUSAGE_WARNING_MB": 1,
                "MEMUSAGE_CHECK_INTERVAL_SECONDS": 0.005,
            },
        ).crawl()
    assert strict_result.reason == "closespider_timeout"
    assert "memusage/limit_reached" not in strict_result.stats
    assert "memusage/warning_reached" not in strict_result.stats


async def _verify_spider_state() -> None:
    with tempfile.TemporaryDirectory() as jobdir:
        first = Crawler(
            StateSpider,
            {
                "ENGINE_BACKEND": "rust",
                "JOBDIR": jobdir,
                "LOGSTATS_INTERVAL": 0,
            },
        )
        await first.crawl()
        assert first.spider is not None
        assert first.spider.state == {"runs": 1}
        assert isinstance(first.extensions.get_by_type(SpiderState), SpiderState)
        assert not (Path(jobdir) / "spider.state").exists()

        second = Crawler(
            StateSpider,
            {
                "ENGINE_BACKEND": "rust",
                "JOBDIR": jobdir,
                "LOGSTATS_INTERVAL": 0,
            },
        )
        await second.crawl()
        assert second.spider is not None
        assert second.spider.state == {"runs": 2}


async def _verify() -> None:
    await _verify_signal_argument_filtering()
    for engine in ("python", "rust"):
        await _verify_core_stats(engine)
        await _verify_log_count(engine)
        await _verify_close_spider(engine)
        await _verify_memory_extensions(engine)
    await _verify_periodic_logging()
    await _verify_spider_state()


if __name__ == "__main__":
    asyncio.run(_verify())
    print(
        "Operational extensions passed: core and periodic stats, close conditions, memory limits, "
        "debugging, task cleanup, and Python/Rust engine parity"
    )
