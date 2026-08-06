from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .downloader import Downloader, create_downloader
from .engine import CrawlEngine, CrawlResult, create_engine
from .settings import Settings
from .signals import SignalManager
from .spider import Spider
from .stats import StatsCollector
from .utils import maybe_await


class Crawler:
    def __init__(
        self,
        spider_cls: type[Spider],
        settings: Settings | Mapping[str, object] | None = None,
        *,
        downloader: Downloader | None = None,
    ) -> None:
        self.spider_cls = spider_cls
        self.settings = settings.copy() if isinstance(settings, Settings) else Settings(settings)
        spider_cls.update_settings(self.settings)
        self.signals = SignalManager()
        self.stats = StatsCollector()
        self.downloader = downloader
        self.spider: Spider | None = None
        self.engine: CrawlEngine | None = None
        self.native_policy_runtime: object | None = None

    async def crawl(self, *args: object, **kwargs: object) -> CrawlResult:
        if self.spider is not None:
            raise RuntimeError("Crawler instances cannot be reused")
        self.spider = self.spider_cls.from_crawler(self, *args, **kwargs)
        self.settings.freeze()
        downloader = self.downloader or create_downloader(self.settings)
        try:
            self.engine = create_engine(self, self.spider, downloader)
        except BaseException:
            close = getattr(downloader, "close", None)
            if close is not None:
                try:
                    await maybe_await(close())
                except Exception:
                    self.stats.inc_value("teardown_errors/count")
                    self.spider.logger.exception(
                        "Error closing downloader after engine construction failed"
                    )
            raise
        return await self.engine.crawl()


class CrawlerRunner:
    def __init__(self, settings: Settings | Mapping[str, object] | None = None) -> None:
        self.settings = settings.copy() if isinstance(settings, Settings) else Settings(settings)
        self.crawlers: set[Crawler] = set()

    async def crawl(
        self,
        spider_cls: type[Spider],
        *args: object,
        downloader: Downloader | None = None,
        **kwargs: Any,
    ) -> CrawlResult:
        crawler = Crawler(spider_cls, self.settings, downloader=downloader)
        self.crawlers.add(crawler)
        try:
            return await crawler.crawl(*args, **kwargs)
        finally:
            self.crawlers.remove(crawler)
