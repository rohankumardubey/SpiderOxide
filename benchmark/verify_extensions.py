from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from spideroxide import (
    Crawler,
    ExtensionManager,
    NotConfigured,
    Request,
    Response,
    Spider,
    signals,
)


def _events(crawler: Crawler) -> list[str]:
    events = getattr(crawler, "extension_events", None)
    if events is None:
        events = []
        crawler.extension_events = events
    return events


class BaseExtension:
    @classmethod
    def from_crawler(cls, crawler: Crawler) -> BaseExtension:
        _events(crawler).append("base:init")
        return cls()


class PathExtension:
    @classmethod
    def from_crawler(cls, crawler: Crawler) -> PathExtension:
        extension = cls()
        extension.crawler = crawler
        _events(crawler).append("path:init")
        crawler.signals.connect(extension.engine_started, signals.engine_started)
        return extension

    def engine_started(self) -> None:
        _events(self.crawler).append("path:engine_started")


class DisabledExtension:
    @classmethod
    def from_crawler(cls, crawler: Crawler) -> DisabledExtension:
        _events(crawler).append("disabled:init")
        raise NotConfigured


class LifecycleExtension:
    @classmethod
    def from_crawler(cls, crawler: Crawler) -> LifecycleExtension:
        extension = cls()
        extension.crawler = crawler
        _events(crawler).append("lifecycle:init")
        crawler.settings.set("EXTENSION_SETTING_APPLIED", True)
        crawler.signals.connect(extension.engine_started, signals.engine_started)
        crawler.signals.connect(extension.spider_opened, signals.spider_opened)
        crawler.signals.connect(extension.request_scheduled, signals.request_scheduled)
        crawler.signals.connect(extension.response_received, signals.response_received)
        crawler.signals.connect(extension.item_scraped, signals.item_scraped)
        crawler.signals.connect(extension.spider_closed, signals.spider_closed)
        crawler.signals.connect(extension.engine_stopped, signals.engine_stopped)
        return extension

    def engine_started(self) -> None:
        _events(self.crawler).append("lifecycle:engine_started")

    def spider_opened(self, spider: Spider) -> None:
        assert spider is self.crawler.spider
        _events(self.crawler).append("lifecycle:spider_opened")

    def request_scheduled(self, request: Request, spider: Spider) -> None:
        assert spider is self.crawler.spider
        assert request.url == "https://example.test/extensions"
        _events(self.crawler).append("lifecycle:request_scheduled")

    def response_received(
        self,
        response: Response,
        request: Request,
        spider: Spider,
    ) -> None:
        assert spider is self.crawler.spider
        assert response.request is request
        _events(self.crawler).append("lifecycle:response_received")

    async def item_scraped(self, item: object, spider: Spider) -> None:
        await asyncio.sleep(0)
        assert item == {"extension": True}
        assert spider is self.crawler.spider
        _events(self.crawler).append("lifecycle:item_scraped")

    def spider_closed(self, spider: Spider, reason: str) -> None:
        assert spider is self.crawler.spider
        assert reason == "finished"
        _events(self.crawler).append("lifecycle:spider_closed")

    def engine_stopped(self) -> None:
        _events(self.crawler).append("lifecycle:engine_stopped")


class InstanceExtension:
    pass


class BrokenExtension:
    @classmethod
    def from_crawler(cls, crawler: Crawler) -> BrokenExtension:
        raise RuntimeError("extension construction failed")


class ExtensionDownloader:
    def __init__(self) -> None:
        self.closed = False

    async def fetch(self, request: Request) -> Response:
        return Response(request.url, request=request)

    async def close(self) -> None:
        self.closed = True


class ExtensionSpider(Spider):
    name = "extensions"
    start_urls = ["https://example.test/extensions"]

    def parse(self, response: Response) -> dict[str, bool]:
        return {"extension": True}

    async def closed(self, reason: str) -> None:
        assert self.crawler is not None
        _events(self.crawler).append("spider:closed")


async def _verify_lifecycle(engine: str) -> None:
    instance = InstanceExtension()
    downloader = ExtensionDownloader()
    crawler = Crawler(
        ExtensionSpider,
        {
            "CONCURRENT_REQUESTS": 1,
            "ENGINE_BACKEND": engine,
            "EXTENSIONS_BASE": {
                BaseExtension: 100,
                DisabledExtension: 200,
            },
            "EXTENSIONS": {
                f"{BaseExtension.__module__}.BaseExtension": None,
                instance: 50,
                "benchmark.verify_extensions.PathExtension": 75,
                LifecycleExtension: 300,
                "optional.extensions.Missing": None,
            },
        },
        downloader=downloader,
    )
    assert crawler.extensions is None
    result = await crawler.crawl()

    assert result.items == ({"extension": True},)
    assert downloader.closed is True
    assert crawler.settings.frozen is True
    assert crawler.settings.getbool("EXTENSION_SETTING_APPLIED") is True
    assert isinstance(crawler.extensions, ExtensionManager)
    assert crawler.extensions.crawler is crawler
    assert len(crawler.extensions) == 3
    loaded = tuple(crawler.extensions)
    assert loaded[0] is instance
    assert type(loaded[1]).__name__ == "PathExtension"
    assert isinstance(loaded[2], LifecycleExtension)
    assert crawler.extensions.get_by_type(InstanceExtension) is instance
    assert crawler.extensions.get_by_type(LifecycleExtension) is loaded[2]
    assert crawler.extensions.get_by_type(BaseExtension) is None

    assert crawler.extension_events == [
        "path:init",
        "disabled:init",
        "lifecycle:init",
        "path:engine_started",
        "lifecycle:engine_started",
        "lifecycle:spider_opened",
        "lifecycle:request_scheduled",
        "lifecycle:response_received",
        "lifecycle:item_scraped",
        "spider:closed",
        "lifecycle:spider_closed",
        "lifecycle:engine_stopped",
    ]


async def _verify_construction_failure() -> None:
    for engine in ("python", "rust"):
        downloader = ExtensionDownloader()
        crawler = Crawler(
            ExtensionSpider,
            {
                "ENGINE_BACKEND": engine,
                "EXTENSIONS": {BrokenExtension: 100},
            },
            downloader=downloader,
        )
        try:
            await crawler.crawl()
        except RuntimeError as error:
            assert str(error) == "extension construction failed"
        else:
            raise AssertionError("crawler accepted a broken extension")
        assert crawler.extensions is None
        assert crawler.engine is None
        assert crawler.settings.frozen is False
        assert downloader.closed is True


async def _verify() -> None:
    for engine in ("python", "rust"):
        await _verify_lifecycle(engine)
    await _verify_construction_failure()


if __name__ == "__main__":
    asyncio.run(_verify())
    print(
        "Extensions passed: priorities, base overrides, factories, instances, opt-outs, "
        "async lifecycle hooks, engine parity, inspection, settings, and failure cleanup"
    )
