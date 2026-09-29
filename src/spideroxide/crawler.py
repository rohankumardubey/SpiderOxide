from __future__ import annotations

import asyncio
import logging
import signal
from collections.abc import Coroutine, Mapping
from functools import partial
from typing import Any, TypeVar

from .addons import AddonManager
from .downloader import Downloader
from .downloadhandlers import DownloadHandlers
from .engine import CrawlEngine, CrawlResult, create_engine
from .extensions import ExtensionManager
from .services import ServiceManager
from .settings import Settings
from .signals import SignalManager
from .spider import Spider
from .spiderloader import SpiderLoader
from .stats import StatsCollector
from .utils import maybe_await

_Component = TypeVar("_Component")


class Crawler:
    def __init__(
        self,
        spidercls: type[Spider],
        settings: Settings | Mapping[str, object] | None = None,
        init_reactor: bool = False,
        *,
        downloader: Downloader | None = None,
    ) -> None:
        if isinstance(spidercls, Spider):
            raise ValueError("The spidercls argument must be a class, not an object")
        self.spidercls = spidercls
        self.spider_cls = spidercls
        self.settings = settings.copy() if isinstance(settings, Settings) else Settings(settings)
        spidercls.update_settings(self.settings)
        self.addons = AddonManager(self)
        self.signals = SignalManager()
        self.stats = StatsCollector()
        self.downloader = downloader
        self.spider: Spider | None = None
        self.engine: CrawlEngine | None = None
        self.extensions: ExtensionManager | None = None
        self.services: ServiceManager | None = None
        self.native_policy_runtime: object | None = None
        self.native_depth_policy: object | None = None
        self.native_download_slots: object | None = None
        self.native_robots_runtime: object | None = None
        self.crawling = False
        self._started = False
        self._init_reactor = init_reactor
        self._crawl_task: asyncio.Task[CrawlResult] | None = None
        self._stop_requested = False
        self.result: CrawlResult | None = None

    async def crawl(self, *args: object, **kwargs: object) -> CrawlResult:
        return await self.crawl_async(*args, **kwargs)

    async def crawl_async(self, *args: object, **kwargs: object) -> CrawlResult:
        if self.crawling:
            raise RuntimeError("Crawling already taking place")
        if self._started:
            raise RuntimeError(
                "Cannot run Crawler.crawl_async() more than once on the same instance."
            )
        self.crawling = self._started = True
        self._crawl_task = asyncio.current_task()
        downloader = self.downloader
        engine_entered = False
        primary_error: BaseException | None = None
        try:
            self.spider = self.spidercls.from_crawler(self, *args, **kwargs)
            self.addons.load_settings(self.settings)
            self.extensions = ExtensionManager.from_crawler(self)
            self.services = ServiceManager.from_crawler(self)
            self.settings.freeze()
            downloader = downloader or DownloadHandlers.from_crawler(self)
            self.downloader = downloader
            self.engine = create_engine(self, self.spider, downloader)
            await self.services.start()
            if self._stop_requested:
                self.engine.close_spider("shutdown")
            engine_entered = True
            self.result = await self.engine.crawl()
            return self.result
        except BaseException as error:
            primary_error = error
            if not engine_entered:
                close = getattr(downloader, "close", None) if downloader is not None else None
                if close is not None:
                    try:
                        await maybe_await(close())
                    except Exception:
                        self.stats.inc_value("teardown_errors/count")
                        logging.getLogger(__name__).exception(
                            "Error closing downloader after crawler construction failed"
                        )
            raise
        finally:
            stop_error: BaseException | None = None
            if self.services is not None:
                try:
                    await self.services.stop()
                except BaseException as error:
                    self.stats.inc_value("teardown_errors/count")
                    if primary_error is None:
                        stop_error = error
                    else:
                        logging.getLogger(__name__).exception("Error stopping crawler services")
            self.crawling = False
            if stop_error is not None:
                raise stop_error

    def stop(self) -> Coroutine[object, object, None]:
        return self.stop_async()

    async def stop_async(self) -> None:
        self._stop_requested = True
        if self.engine is not None:
            self.engine.close_spider("shutdown")
        task = self._crawl_task
        if task is not None and task is not asyncio.current_task() and not task.done():
            await asyncio.shield(task)

    @staticmethod
    def _get_component(
        component_class: type[_Component],
        components: object,
    ) -> _Component | None:
        return next(
            (
                component
                for component in components  # type: ignore[union-attr]
                if isinstance(component, component_class)
            ),
            None,
        )

    def get_extension(self, cls: type[_Component]) -> _Component | None:
        if self.extensions is None:
            raise RuntimeError(
                "Crawler.get_extension() can only be called after "
                "the extension manager has been created."
            )
        return self._get_component(cls, self.extensions.middlewares)

    def get_addon(self, cls: type[_Component]) -> _Component | None:
        return self._get_component(cls, self.addons.addons)

    def get_service(self, cls: type[_Component]) -> _Component | None:
        if self.services is None:
            raise RuntimeError(
                "Crawler.get_service() can only be called after "
                "the service manager has been created."
            )
        return self._get_component(cls, self.services.services)

    def get_downloader_middleware(self, cls: type[_Component]) -> _Component | None:
        if self.engine is None:
            raise RuntimeError(
                "Crawler.get_downloader_middleware() can only be called after "
                "the crawl engine has been created."
            )
        return self._get_component(cls, self.engine.downloader_middleware.middleware)

    def get_item_pipeline(self, cls: type[_Component]) -> _Component | None:
        if self.engine is None:
            raise RuntimeError(
                "Crawler.get_item_pipeline() can only be called after "
                "the crawl engine has been created."
            )
        return self._get_component(cls, self.engine.item_pipelines.pipelines)

    def get_spider_middleware(self, cls: type[_Component]) -> _Component | None:
        if self.engine is None:
            raise RuntimeError(
                "Crawler.get_spider_middleware() can only be called after "
                "the crawl engine has been created."
            )
        return self._get_component(cls, self.engine.spider_middleware.middleware)


class AsyncCrawlerRunner:
    def __init__(self, settings: Settings | Mapping[str, object] | None = None) -> None:
        self.settings = settings.copy() if isinstance(settings, Settings) else Settings(settings)
        AddonManager.load_pre_crawler_settings(self.settings)
        self.spider_loader = SpiderLoader.from_settings(self.settings)
        self._crawlers: set[Crawler] = set()
        self._active: set[asyncio.Task[CrawlResult]] = set()
        self.bootstrap_failed = False

    @property
    def crawlers(self) -> set[Crawler]:
        return self._crawlers

    def create_crawler(
        self,
        crawler_or_spidercls: type[Spider] | str | Crawler,
        *,
        downloader: Downloader | None = None,
    ) -> Crawler:
        if isinstance(crawler_or_spidercls, Spider):
            raise ValueError(
                "The crawler_or_spidercls argument cannot be a spider object, "
                "it must be a spider class (or a Crawler object)"
            )
        if isinstance(crawler_or_spidercls, Crawler):
            for name in self.settings:
                priority = self.settings.getpriority(name) or 0
                crawler_priority = crawler_or_spidercls.settings.getpriority(name)
                if crawler_priority is None or crawler_priority < priority:
                    crawler_or_spidercls.settings.set(name, self.settings[name], priority)
            if downloader is not None:
                crawler_or_spidercls.downloader = downloader
            return crawler_or_spidercls
        spidercls = (
            self.spider_loader.load(crawler_or_spidercls)
            if isinstance(crawler_or_spidercls, str)
            else crawler_or_spidercls
        )
        return Crawler(spidercls, self.settings, downloader=downloader)

    def crawl(
        self,
        crawler_or_spidercls: type[Spider] | str | Crawler,
        *args: object,
        **kwargs: Any,
    ) -> asyncio.Task[CrawlResult]:
        downloader = kwargs.pop("downloader", None)
        crawler = self.create_crawler(
            crawler_or_spidercls,
            downloader=downloader,
        )
        return self._crawl(crawler, *args, **kwargs)

    def _crawl(
        self,
        crawler: Crawler,
        *args: object,
        **kwargs: object,
    ) -> asyncio.Task[CrawlResult]:
        loop = asyncio.get_event_loop()
        self._crawlers.add(crawler)
        task = loop.create_task(crawler.crawl_async(*args, **kwargs))
        self._active.add(task)
        task.add_done_callback(partial(self._done, crawler=crawler))
        return task

    def _done(
        self,
        task: asyncio.Task[CrawlResult],
        *,
        crawler: Crawler,
    ) -> None:
        self._active.discard(task)
        self._crawlers.discard(crawler)
        self.bootstrap_failed |= crawler.spider is None

    async def stop(self) -> None:
        if self._crawlers:
            await asyncio.gather(
                *(crawler.stop_async() for crawler in tuple(self._crawlers)),
                return_exceptions=False,
            )

    async def join(self) -> None:
        while self._active:
            await asyncio.wait(tuple(self._active))


class CrawlerRunner(AsyncCrawlerRunner):
    pass


class AsyncCrawlerProcess(AsyncCrawlerRunner):
    def __init__(
        self,
        settings: Settings | Mapping[str, object] | None = None,
        install_root_handler: bool | None = None,
    ) -> None:
        del install_root_handler
        self._loop = asyncio.new_event_loop()
        self._closed = False
        self._main_task: asyncio.Future[None] | None = None
        self._stop_after_crawl = True
        super().__init__(settings)

    def _crawl(
        self,
        crawler: Crawler,
        *args: object,
        **kwargs: object,
    ) -> asyncio.Task[CrawlResult]:
        if self._closed:
            raise RuntimeError("Crawler process has already been closed")
        self._crawlers.add(crawler)
        task = self._loop.create_task(crawler.crawl_async(*args, **kwargs))
        self._active.add(task)
        task.add_done_callback(partial(self._done, crawler=crawler))
        return task

    def start(
        self,
        stop_after_crawl: bool = True,
        install_signal_handlers: bool = True,
    ) -> None:
        if self._closed:
            raise RuntimeError("Crawler process has already been closed")
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            raise RuntimeError(
                "AsyncCrawlerProcess.start() cannot run inside an active event loop; "
                "use AsyncCrawlerRunner instead"
            )
        self._stop_after_crawl = stop_after_crawl
        self._main_task = (
            self._loop.create_task(self.join()) if stop_after_crawl else self._loop.create_future()
        )
        previous_handlers: dict[signal.Signals, object] = {}
        if install_signal_handlers:
            for signum in (signal.SIGINT, signal.SIGTERM):
                previous_handlers[signum] = signal.getsignal(signum)
                signal.signal(signum, self._signal_shutdown)
        try:
            self._loop.run_until_complete(self._main_task)
        except asyncio.CancelledError:
            pass
        finally:
            for signum, handler in previous_handlers.items():
                signal.signal(signum, handler)
            self._close_loop()

    def _signal_shutdown(self, signum: int, frame: object) -> None:
        del signum, frame
        for current in (signal.SIGINT, signal.SIGTERM):
            signal.signal(current, self._signal_kill)
        self._loop.call_soon_threadsafe(lambda: self._loop.create_task(self._graceful_shutdown()))

    def _signal_kill(self, signum: int, frame: object) -> None:
        del signum, frame
        if self._main_task is not None:
            self._loop.call_soon_threadsafe(self._main_task.cancel)

    async def _graceful_shutdown(self) -> None:
        await self.stop()
        await self.join()
        if not self._stop_after_crawl and self._main_task is not None:
            self._main_task.cancel()

    def _close_loop(self) -> None:
        pending = asyncio.all_tasks(self._loop)
        for task in pending:
            task.cancel()
        if pending:
            self._loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        self._loop.run_until_complete(self._loop.shutdown_asyncgens())
        self._loop.run_until_complete(self._loop.shutdown_default_executor())
        self._loop.close()
        self._closed = True


class CrawlerProcess(AsyncCrawlerProcess):
    pass


__all__ = [
    "AsyncCrawlerProcess",
    "AsyncCrawlerRunner",
    "Crawler",
    "CrawlerProcess",
    "CrawlerRunner",
]
