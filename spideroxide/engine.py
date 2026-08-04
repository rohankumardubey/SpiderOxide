from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass

from . import signals
from .api import Scheduler
from .downloader import Downloader
from .exceptions import CloseSpider, DropItem, IgnoreRequest
from .http import Request, Response
from .middleware import (
    DownloaderMiddlewareManager,
    ItemPipelineManager,
    SpiderMiddlewareManager,
)
from .settings import Settings
from .spider import Spider
from .stats import StatsCollector
from .utils import collect_outputs, maybe_await


@dataclass(frozen=True, slots=True)
class CrawlResult:
    reason: str
    items: tuple[object, ...]
    stats: dict[str, object]


@dataclass(frozen=True, slots=True)
class _StartFailure:
    exception: BaseException


class CrawlEngine:
    def __init__(self, crawler: object, spider: Spider, downloader: Downloader) -> None:
        self.crawler = crawler
        self.spider = spider
        self.downloader = downloader
        self.settings: Settings = crawler.settings  # type: ignore[attr-defined]
        self.signals = crawler.signals  # type: ignore[attr-defined]
        self.stats: StatsCollector = crawler.stats  # type: ignore[attr-defined]
        self.scheduler = Scheduler()
        self.downloader_middleware = DownloaderMiddlewareManager(
            crawler,
            self.settings.get("DOWNLOADER_MIDDLEWARES", []),
        )
        self.spider_middleware = SpiderMiddlewareManager(
            crawler,
            self.settings.get("SPIDER_MIDDLEWARES", []),
        )
        self.item_pipelines = ItemPipelineManager(
            crawler,
            self.settings.get("ITEM_PIPELINES", []),
        )
        self.concurrent_requests = self.settings.getint("CONCURRENT_REQUESTS", 16)
        if self.concurrent_requests < 1:
            raise ValueError("CONCURRENT_REQUESTS must be at least 1")
        self.items: list[object] = []

    async def crawl(self) -> CrawlResult:
        tasks: set[asyncio.Task[list[object]]] = set()
        start_producer: asyncio.Task[None] | None = None
        next_start: asyncio.Task[Request | _StartFailure | None] | None = None
        spider_opened = False
        reason = "finished"
        try:
            self.stats.set_value("start_time", asyncio.get_running_loop().time())
            await self.signals.send(signals.engine_started)
            await self.signals.send(signals.spider_opened, spider=self.spider)
            spider_opened = True

            start_queue: asyncio.Queue[Request | _StartFailure | None] = asyncio.Queue(
                maxsize=self.concurrent_requests * 2
            )
            start_producer = asyncio.create_task(self._produce_start_requests(start_queue))
            next_start = asyncio.create_task(start_queue.get())

            while len(self.scheduler) or tasks or next_start is not None:
                while len(tasks) < self.concurrent_requests:
                    request = self.scheduler.pop()
                    if request is None:
                        break
                    if not isinstance(request, Request):
                        raise TypeError("scheduler returned an unsupported request type")
                    tasks.add(asyncio.create_task(self._handle_request(request)))

                waiters: set[asyncio.Task[object]] = set(tasks)
                if next_start is not None:
                    waiters.add(next_start)
                if not waiters:
                    break
                done, _ = await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)

                if next_start is not None and next_start in done:
                    start_output = next_start.result()
                    if isinstance(start_output, _StartFailure):
                        raise start_output.exception
                    if start_output is None:
                        next_start = None
                    else:
                        await self._schedule(start_output)
                        next_start = asyncio.create_task(start_queue.get())

                completed_requests = tasks.intersection(done)
                tasks.difference_update(completed_requests)
                for task in completed_requests:
                    outputs = task.result()
                    await self._process_outputs(outputs)
        except CloseSpider as exception:
            reason = exception.reason
        except asyncio.CancelledError:
            reason = "cancelled"
            raise
        except BaseException:
            reason = "error"
            raise
        finally:
            pending: list[asyncio.Task[object]] = list(tasks)
            if next_start is not None:
                pending.append(next_start)
            if start_producer is not None:
                pending.append(start_producer)
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)

            self.stats.set_value("finish_time", asyncio.get_running_loop().time())
            self.stats.set_value("finish_reason", reason)
            close = getattr(self.downloader, "close", None)
            if close is not None:
                await self._safe_teardown("downloader/close", close)
            if spider_opened:
                await self._safe_teardown("spider/closed", self.spider.closed, reason)
                await self._safe_teardown(
                    "signal/spider_closed",
                    self.signals.send,
                    signals.spider_closed,
                    spider=self.spider,
                    reason=reason,
                )
            await self._safe_teardown(
                "signal/engine_stopped",
                self.signals.send,
                signals.engine_stopped,
            )
        return CrawlResult(reason, tuple(self.items), dict(self.stats.get_stats()))

    async def _produce_start_requests(
        self,
        queue: asyncio.Queue[Request | _StartFailure | None],
    ) -> None:
        try:
            async for request in self.spider.start():
                if not isinstance(request, Request):
                    raise TypeError("Spider.start() must yield Request objects")
                await queue.put(request)
        except BaseException as exception:
            await queue.put(_StartFailure(exception))
        else:
            await queue.put(None)

    async def _safe_teardown(
        self,
        name: str,
        function: Callable[..., object],
        *args: object,
        **kwargs: object,
    ) -> None:
        try:
            await maybe_await(function(*args, **kwargs))
        except Exception:
            self.stats.inc_value("teardown_errors/count")
            self.spider.logger.exception("Error during %s", name)

    async def _schedule(self, request: Request) -> bool:
        if not isinstance(request, Request):
            raise TypeError("spider output must contain Request objects or items")
        inserted = self.scheduler.push_request(request)
        if inserted:
            self.stats.inc_value("scheduler/enqueued")
            await self.signals.send(
                signals.request_scheduled,
                request=request,
                spider=self.spider,
            )
        else:
            self.stats.inc_value("dupefilter/filtered")
            await self.signals.send(
                signals.request_dropped,
                request=request,
                spider=self.spider,
            )
        return inserted

    async def _handle_request(self, request: Request) -> list[object]:
        self.stats.inc_value("downloader/request_count")
        try:
            downloaded = await self.downloader_middleware.download(request, self.downloader.fetch)
        except CloseSpider:
            raise
        except IgnoreRequest:
            self.stats.inc_value("downloader/exception_count")
            return []
        except Exception as exception:
            self.stats.inc_value("downloader/exception_count")
            return await self._run_errback(request, exception)

        if isinstance(downloaded, Request):
            return [downloaded]
        response = downloaded
        self.stats.inc_value("downloader/response_count")
        self.stats.inc_value(f"downloader/response_status_count/{response.status}")
        await self.signals.send(
            signals.response_received,
            response=response,
            request=request,
            spider=self.spider,
        )
        return await self._run_callback(request, response)

    async def _run_callback(self, request: Request, response: Response) -> list[object]:
        try:
            await self.spider_middleware.process_input(response, self.spider)
            callback = request.callback or self.spider.parse
            outputs = await collect_outputs(callback(response, **request.cb_kwargs))
            return await self.spider_middleware.process_output(
                response,
                outputs,
                self.spider,
            )
        except CloseSpider:
            raise
        except Exception as exception:
            recovered = await self.spider_middleware.process_exception(
                response,
                exception,
                self.spider,
            )
            if recovered is not None:
                return recovered
            return await self._run_errback(request, exception, response=response)

    async def _run_errback(
        self,
        request: Request,
        exception: Exception,
        *,
        response: Response | None = None,
    ) -> list[object]:
        if request.errback is not None:
            try:
                return await collect_outputs(request.errback(exception))
            except CloseSpider:
                raise
            except Exception as errback_exception:
                exception = errback_exception
        self.stats.inc_value("spider_exceptions/count")
        kwargs: dict[str, object] = {
            "failure": exception,
            "request": request,
            "spider": self.spider,
        }
        if response is not None:
            kwargs["response"] = response
        await self.signals.send(signals.spider_error, **kwargs)
        return []

    async def _process_outputs(self, outputs: list[object]) -> None:
        for output in outputs:
            if isinstance(output, Request):
                await self._schedule(output)
                continue
            try:
                item = await self.item_pipelines.process_item(output, self.spider)
            except DropItem as exception:
                self.stats.inc_value("item_dropped_count")
                await self.signals.send(
                    signals.item_dropped,
                    item=output,
                    exception=exception,
                    spider=self.spider,
                )
                continue
            self.items.append(item)
            self.stats.inc_value("item_scraped_count")
            await self.signals.send(signals.item_scraped, item=item, spider=self.spider)
