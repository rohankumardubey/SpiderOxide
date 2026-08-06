from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from spideroxide import (
    Crawler,
    DownloadError,
    DropItem,
    Headers,
    HttpxDownloader,
    NotConfigured,
    Request,
    Response,
    Scheduler,
    Settings,
    Spider,
    TextResponse,
    signals,
)


class FakeDownloader:
    def __init__(self, *, require_middleware: bool = False) -> None:
        self.history: list[Request] = []
        self.closed = False
        self.require_middleware = require_middleware

    async def fetch(self, request: Request) -> Response:
        self.history.append(request)
        if request.url.endswith("/fail"):
            raise DownloadError("expected failure")
        if self.require_middleware:
            assert request.headers["X-Middleware"] == b"enabled"
        return TextResponse(
            request.url,
            headers=Headers({"Content-Type": "text/html; charset=utf-8"}),
            body=f"<title>{request.url}</title>".encode(),
            request=request,
        )

    async def close(self) -> None:
        self.closed = True


class DisabledMiddleware:
    @classmethod
    def from_crawler(cls, crawler: Crawler) -> DisabledMiddleware:
        raise NotConfigured


class TestDownloaderMiddleware:
    def process_request(self, request: Request, spider: Spider) -> None:
        assert spider.name == "foundation"
        request.headers["X-Middleware"] = b"enabled"
        return None

    def process_response(
        self,
        request: Request,
        response: Response,
        spider: Spider,
    ) -> Response:
        assert response.request is request
        return response.replace(flags=(*response.flags, "middleware"))


class TestSpiderMiddleware:
    def process_spider_output(
        self,
        response: Response,
        outputs: list[object],
        spider: Spider,
    ) -> list[object]:
        assert "middleware" in response.flags
        processed = []
        for output in outputs:
            if isinstance(output, dict):
                processed.append({**output, "spider_middleware": True})
            else:
                processed.append(output)
        return processed


class TestPipeline:
    def process_item(self, item: object, spider: Spider) -> object:
        assert isinstance(item, dict)
        if item.get("drop"):
            raise DropItem("fixture item")
        return {**item, "pipeline": True}


class FoundationSpider(Spider):
    name = "foundation"
    start_urls = ["https://example.test/start"]
    custom_settings = {
        "CONCURRENT_REQUESTS": 2,
        "DOWNLOADER_MIDDLEWARES": [DisabledMiddleware, TestDownloaderMiddleware],
        "SPIDER_MIDDLEWARES": [TestSpiderMiddleware],
        "ITEM_PIPELINES": [TestPipeline],
        "RETRY_ENABLED": False,
    }

    def parse(self, response: Response) -> list[object]:
        return [
            {"source": "start"},
            response.follow("/high", callback=self.parse_page, priority=10),
            response.follow("/high", callback=self.parse_page, priority=99),
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
            response.follow("/low", callback=self.parse_page, priority=1),
            response.follow("/fail", errback=self.handle_error),
        ]

    def parse_page(self, response: Response) -> dict[str, object]:
        assert isinstance(response, TextResponse)
        assert response.text.startswith("<title>")
        return {"url": response.url, "drop": response.url.endswith("/low")}

    def handle_error(self, exception: BaseException) -> dict[str, object]:
        return {"error": str(exception)}


def _verify_models() -> None:
    headers = Headers({"Content-Type": "text/plain", "X-Test": ["one", b"two"]})
    assert headers["content-type"] == b"text/plain"
    assert headers.getlist("x-test") == [b"one", b"two"]
    headers.appendlist("X-Test", "three")
    assert headers["X-TEST"] == b"three"

    request = Request(
        "https://example.test/path",
        method="post",
        body=b"payload",
        headers=headers,
        meta={"depth": 1},
    )
    copied = request.replace(priority=10)
    assert copied.method == "POST"
    assert copied.priority == 10
    assert copied.meta == request.meta
    assert copied.meta is not request.meta
    assert request.follow("../next").url == "https://example.test/next"

    response = TextResponse(
        request.url,
        headers=Headers({"Content-Type": "application/json; charset=utf-8"}),
        body=b'{"ok": true}',
        request=request,
    )
    assert response.json() == {"ok": True}
    assert response.meta["depth"] == 1
    assert response.follow("/child").url == "https://example.test/child"
    for invalid_url in ("https:///missing", "mailto:test@example.test", "/relative"):
        try:
            Request(invalid_url)
        except ValueError:
            pass
        else:
            raise AssertionError(f"invalid URL was accepted: {invalid_url}")


def _verify_scheduler_identity() -> None:
    for backend in ("python", "rust"):
        scheduler = Scheduler(backend)
        low = Request(
            "https://example.test/repeated",
            priority=1,
            dont_filter=True,
            meta={"id": "low"},
        )
        high = low.replace(priority=10, meta={"id": "high"})
        assert scheduler.push_request(low)
        assert scheduler.push_request(high)
        assert scheduler.pop() is high
        assert scheduler.pop() is low


def _verify_settings() -> None:
    settings = Settings({"VALUE": "project"})
    settings.set("VALUE", "default", priority="default")
    assert settings["VALUE"] == "project"
    settings.set("VALUE", "spider", priority="spider")
    assert settings["VALUE"] == "spider"
    settings.set("BOOL", "yes")
    assert settings.getbool("BOOL") is True
    settings.freeze()
    try:
        settings["VALUE"] = "forbidden"
    except TypeError:
        pass
    else:
        raise AssertionError("frozen settings must reject writes")


async def _verify_engine() -> None:
    downloader = FakeDownloader(require_middleware=True)
    crawler = Crawler(
        FoundationSpider,
        {"CONCURRENT_REQUESTS": 8},
        downloader=downloader,
    )
    scraped_items: list[object] = []

    async def item_scraped(item: object, spider: Spider) -> None:
        assert spider.name == "foundation"
        scraped_items.append(item)

    crawler.signals.connect(item_scraped, signals.item_scraped)
    result = await crawler.crawl()

    assert result.reason == "finished"
    assert len(result.items) == 5
    assert result.items == tuple(scraped_items)
    assert all(isinstance(item, dict) and item["pipeline"] is True for item in result.items)
    assert crawler.settings.getint("CONCURRENT_REQUESTS") == 2

    urls = [request.url for request in downloader.history]
    assert urls[0] == "https://example.test/start"
    assert urls.count("https://example.test/high") == 1
    assert urls.count("https://example.test/repeat") == 2
    assert result.stats["scheduler/enqueued"] == 6
    assert result.stats["dupefilter/filtered"] == 1
    assert result.stats["item_scraped_count"] == 5
    assert result.stats["item_dropped_count"] == 1
    assert result.stats["downloader/exception_count"] == 1
    assert downloader.closed is True


class StreamingStartSpider(Spider):
    name = "streaming"

    def __init__(self) -> None:
        super().__init__()
        self.first_processed = asyncio.Event()

    async def start(self):
        yield Request(
            "https://example.test/stream-one",
            callback=self.parse_first,
            dont_filter=True,
        )
        await self.first_processed.wait()
        yield Request(
            "https://example.test/stream-two",
            callback=self.parse_second,
            dont_filter=True,
        )

    def parse_first(self, response: Response) -> dict[str, str]:
        self.first_processed.set()
        return {"url": response.url}

    def parse_second(self, response: Response) -> dict[str, str]:
        return {"url": response.url}


class ErrbackSpider(Spider):
    name = "errback"

    def __init__(self) -> None:
        super().__init__()
        self.errback_calls = 0

    async def start(self):
        yield Request(
            "https://example.test/callback-error",
            callback=self.fail_callback,
            errback=self.fail_errback,
            dont_filter=True,
        )

    def fail_callback(self, response: Response) -> object:
        raise ValueError("callback failed")

    def fail_errback(self, exception: BaseException) -> object:
        self.errback_calls += 1
        raise RuntimeError("errback failed")


async def _verify_streaming_start_and_errback() -> None:
    streaming_downloader = FakeDownloader()
    streaming = Crawler(StreamingStartSpider, downloader=streaming_downloader)
    streaming_result = await asyncio.wait_for(streaming.crawl(), timeout=2)
    assert len(streaming_result.items) == 2
    assert streaming_downloader.closed is True

    errback_downloader = FakeDownloader()
    errback = Crawler(ErrbackSpider, downloader=errback_downloader)
    errback_result = await errback.crawl()
    assert errback.spider is not None
    assert errback.spider.errback_calls == 1
    assert errback_result.stats["spider_exceptions/count"] == 1
    assert errback_downloader.closed is True


async def _verify_http_transport() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        repeated = [
            value for name, value in request.headers.multi_items() if name.lower() == "x-repeat"
        ]
        assert repeated == ["one", "two"]
        return httpx.Response(
            200,
            headers=[
                ("Content-Type", "application/json; charset=utf-8"),
                ("Set-Cookie", "a=1"),
                ("Set-Cookie", "b=2"),
            ],
            content=b'{"transport": true}',
        )

    downloader = HttpxDownloader(transport=httpx.MockTransport(handler))
    response = await downloader.fetch(
        Request(
            "https://example.test/transport",
            headers=Headers({"X-Repeat": ["one", "two"]}),
        )
    )
    assert isinstance(response, TextResponse)
    assert response.json() == {"transport": True}
    assert response.headers.getlist("set-cookie") == [b"a=1", b"b=2"]
    await downloader.close()


def run_crawler_checks() -> dict[str, object]:
    _verify_models()
    _verify_scheduler_identity()
    _verify_settings()
    asyncio.run(_verify_engine())
    asyncio.run(_verify_streaming_start_and_errback())
    asyncio.run(_verify_http_transport())
    return {
        "passed": True,
        "models": ["Headers", "Request", "Response", "TextResponse"],
        "runtime": ["settings", "signals", "middleware", "pipelines", "engine"],
    }


if __name__ == "__main__":
    result = run_crawler_checks()
    print(
        f"Crawler foundation passed: {len(result['models'])} HTTP models and "
        f"{len(result['runtime'])} runtime subsystems"
    )
