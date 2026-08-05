from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from spideroxide._native import NativeCrawlCoordinator

from spideroxide import (
    CloseSpider,
    Crawler,
    DownloadError,
    NativeCrawlEngine,
    Request,
    Response,
    Spider,
    TextResponse,
    signals,
)


class RecordingDownloader:
    def __init__(self, *, delay: float = 0.005) -> None:
        self.delay = delay
        self.history: list[Request] = []
        self.active = 0
        self.max_active = 0
        self.started = asyncio.Event()
        self.closed = False

    async def fetch(self, request: Request) -> Response:
        self.history.append(request)
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        self.started.set()
        try:
            if request.url.endswith("/fail"):
                raise DownloadError("expected native engine failure")
            await asyncio.sleep(self.delay)
            return TextResponse(
                request.url,
                body=request.url.encode(),
                request=request,
            )
        finally:
            self.active -= 1

    async def close(self) -> None:
        self.closed = True


class BlockingDownloader(RecordingDownloader):
    def __init__(self) -> None:
        super().__init__(delay=0)
        self.release = asyncio.Event()

    async def fetch(self, request: Request) -> Response:
        self.history.append(request)
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        self.started.set()
        try:
            await self.release.wait()
            return TextResponse(request.url, request=request)
        finally:
            self.active -= 1


async def _verify_coordinator() -> None:
    coordinator = NativeCrawlCoordinator(1, 2)
    low = coordinator.schedule("https://example.test/low", "GET", b"", "1", True)
    huge_priority = "1" + ("0" * 100)
    high_one = coordinator.schedule(
        "https://example.test/high-one",
        "GET",
        b"",
        huge_priority,
        True,
    )
    high_two = coordinator.schedule(
        "https://example.test/high-two",
        "GET",
        b"",
        huge_priority,
        True,
    )
    assert (low, high_one, high_two) == (0, 1, 2)
    assert coordinator.schedule("https://example.test/low", "GET", b"", "99", True) is None
    repeated = coordinator.schedule("https://example.test/low", "GET", b"", "5", False)
    assert repeated == 3
    assert coordinator.queued_count == 4
    assert coordinator.seen_count == 3
    for request_id in (low, high_one, high_two, repeated):
        coordinator.activate(request_id)
    coordinator.close_input()

    order = []
    while (request_id := await coordinator.next_request()) is not None:
        order.append(request_id)
        assert coordinator.active_count == 1
        coordinator.complete(request_id)
    assert order == [high_one, high_two, repeated, low]
    assert coordinator.active_count == 0
    assert coordinator.queued_count == 0


class PrioritySpider(Spider):
    name = "native-priority"
    start_urls = ["https://example.test/root"]

    def parse(self, response: Response) -> list[object]:
        return [
            response.follow("/low", callback=self.parse_page, priority=1),
            response.follow("/huge", callback=self.parse_page, priority=10**100),
            response.follow("/high-one", callback=self.parse_page, priority=10),
            response.follow("/high-two", callback=self.parse_page, priority=10),
            response.follow("/high-one", callback=self.parse_page, priority=99),
            response.follow(
                "/repeat",
                callback=self.parse_page,
                priority=5,
                dont_filter=True,
            ),
            response.follow(
                "/repeat",
                callback=self.parse_page,
                priority=5,
                dont_filter=True,
            ),
            response.follow("/fail", errback=self.handle_error),
        ]

    def parse_page(self, response: Response) -> dict[str, str]:
        return {"url": response.url}

    def handle_error(self, exception: BaseException) -> dict[str, str]:
        return {"error": str(exception)}


async def _verify_engine_behavior() -> None:
    downloader = RecordingDownloader()
    crawler = Crawler(
        PrioritySpider,
        {
            "CONCURRENT_REQUESTS": 1,
            "ENGINE_BACKEND": "rust",
            "ENGINE_MAX_PENDING": 2,
        },
        downloader=downloader,
    )
    result = await crawler.crawl()
    assert isinstance(crawler.engine, NativeCrawlEngine)
    assert crawler.engine.backend_name == "rust"
    assert result.reason == "finished"
    assert downloader.closed is True
    urls = [request.url for request in downloader.history]
    assert urls == [
        "https://example.test/root",
        "https://example.test/huge",
        "https://example.test/high-one",
        "https://example.test/high-two",
        "https://example.test/repeat",
        "https://example.test/repeat",
        "https://example.test/low",
        "https://example.test/fail",
    ]
    assert result.stats["scheduler/enqueued"] == 8
    assert result.stats["dupefilter/filtered"] == 1
    assert result.stats["item_scraped_count"] == 7
    assert result.stats["downloader/exception_count"] == 1


class ConcurrencySpider(Spider):
    name = "native-concurrency"
    start_urls = [f"https://example.test/{index}" for index in range(12)]

    def parse(self, response: Response) -> dict[str, str]:
        return {"url": response.url}


async def _verify_concurrency() -> None:
    downloader = RecordingDownloader(delay=0.02)
    result = await Crawler(
        ConcurrencySpider,
        {
            "CONCURRENT_REQUESTS": 3,
            "ENGINE_BACKEND": "rust",
            "ENGINE_MAX_PENDING": 2,
        },
        downloader=downloader,
    ).crawl()
    assert len(result.items) == 12
    assert downloader.max_active == 3
    assert downloader.closed is True


class BackpressureSpider(Spider):
    name = "native-backpressure"

    def __init__(self) -> None:
        super().__init__()
        self.produced = 0

    async def start(self):
        for index in range(20):
            self.produced += 1
            yield Request(f"https://example.test/{index}", dont_filter=True)

    def parse(self, response: Response) -> None:
        return None


async def _verify_backpressure() -> None:
    downloader = BlockingDownloader()
    crawler = Crawler(
        BackpressureSpider,
        {
            "CONCURRENT_REQUESTS": 1,
            "ENGINE_BACKEND": "rust",
            "ENGINE_MAX_PENDING": 1,
        },
        downloader=downloader,
    )
    crawl_task = asyncio.create_task(crawler.crawl())
    await asyncio.wait_for(downloader.started.wait(), timeout=1)
    await asyncio.sleep(0.02)
    assert crawler.spider is not None
    assert crawler.spider.produced <= 3
    downloader.release.set()
    result = await asyncio.wait_for(crawl_task, timeout=2)
    assert result.stats["downloader/request_count"] == 20


class StreamingSpider(Spider):
    name = "native-streaming"

    def __init__(self) -> None:
        super().__init__()
        self.first_processed = asyncio.Event()

    async def start(self):
        yield Request(
            "https://example.test/first",
            callback=self.parse_first,
            dont_filter=True,
        )
        await self.first_processed.wait()
        yield Request(
            "https://example.test/second",
            callback=self.parse_second,
            dont_filter=True,
        )

    def parse_first(self, response: Response) -> dict[str, str]:
        self.first_processed.set()
        return {"url": response.url}

    def parse_second(self, response: Response) -> dict[str, str]:
        return {"url": response.url}


async def _verify_streaming_start() -> None:
    result = await asyncio.wait_for(
        Crawler(
            StreamingSpider,
            {"ENGINE_BACKEND": "rust"},
            downloader=RecordingDownloader(),
        ).crawl(),
        timeout=2,
    )
    assert len(result.items) == 2


class ClosingSpider(Spider):
    name = "native-close"
    start_urls = ["https://example.test/close"]

    def parse(self, response: Response) -> None:
        raise CloseSpider("complete")


async def _verify_close_and_cancellation() -> None:
    close_downloader = RecordingDownloader()
    result = await Crawler(
        ClosingSpider,
        {"ENGINE_BACKEND": "rust"},
        downloader=close_downloader,
    ).crawl()
    assert result.reason == "complete"
    assert result.stats["finish_reason"] == "complete"
    assert close_downloader.closed is True

    blocking = BlockingDownloader()
    crawler = Crawler(
        ConcurrencySpider,
        {"ENGINE_BACKEND": "rust"},
        downloader=blocking,
    )
    task = asyncio.create_task(crawler.crawl())
    await asyncio.wait_for(blocking.started.wait(), timeout=1)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    else:
        raise AssertionError("cancelled native crawl did not propagate cancellation")
    assert crawler.stats.get_value("finish_reason") == "cancelled"
    assert blocking.closed is True


async def _verify_construction_cleanup() -> None:
    downloader = RecordingDownloader()
    crawler = Crawler(
        ConcurrencySpider,
        {
            "ENGINE_BACKEND": "rust",
            "ENGINE_MAX_PENDING": -1,
        },
        downloader=downloader,
    )
    try:
        await crawler.crawl()
    except ValueError as error:
        assert "ENGINE_MAX_PENDING" in str(error)
    else:
        raise AssertionError("native engine accepted a negative pending limit")
    assert downloader.closed is True

    invalid_downloader = RecordingDownloader()
    invalid = Crawler(
        ConcurrencySpider,
        {"ENGINE_BACKEND": "invalid"},
        downloader=invalid_downloader,
    )
    try:
        await invalid.crawl()
    except ValueError as error:
        assert "invalid engine backend" in str(error)
    else:
        raise AssertionError("crawler accepted an invalid engine backend")
    assert invalid_downloader.closed is True


async def _verify_signal_ordering() -> None:
    events: list[str] = []

    class SignalDownloader(RecordingDownloader):
        async def fetch(self, request: Request) -> Response:
            events.append(f"fetch:{request.url}")
            return await super().fetch(request)

    async def scheduled(request: Request, spider: Spider) -> None:
        assert spider.name == "native-concurrency"
        events.append(f"signal-start:{request.url}")
        await asyncio.sleep(0.01)
        events.append(f"signal-end:{request.url}")

    downloader = SignalDownloader()
    crawler = Crawler(
        ConcurrencySpider,
        {
            "CONCURRENT_REQUESTS": 1,
            "ENGINE_BACKEND": "rust",
        },
        downloader=downloader,
    )
    crawler.signals.connect(scheduled, signals.request_scheduled)
    await crawler.crawl()
    for request in downloader.history:
        assert events.index(f"signal-end:{request.url}") < events.index(f"fetch:{request.url}")


async def _verify() -> None:
    await _verify_coordinator()
    await _verify_engine_behavior()
    await _verify_concurrency()
    await _verify_backpressure()
    await _verify_streaming_start()
    await _verify_close_and_cancellation()
    await _verify_construction_cleanup()
    await _verify_signal_ordering()


if __name__ == "__main__":
    asyncio.run(_verify())
    print(
        "Native engine passed: priority, duplicates, concurrency, backpressure, streaming, "
        "errors, cancellation, and lifecycle"
    )
