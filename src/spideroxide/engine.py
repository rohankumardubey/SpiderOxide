from __future__ import annotations

import asyncio
import os
import warnings
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from urllib.parse import urlsplit

from . import signals
from ._scheduler import EngineScheduler, SchedulerQueueConfig
from .backend import BackendUnavailableError
from .downloader import Downloader
from .exceptions import CloseSpider, DropItem, IgnoreRequest
from .http import Request, Response
from .job import (
    deserialize_request,
    deserialize_spider_state,
    serialize_request,
    serialize_spider_state,
)
from .middleware import (
    DownloaderMiddlewareManager,
    ItemPipelineManager,
    SpiderMiddlewareManager,
    _InvalidMiddlewareOutput,
    _UnhandledSpiderMiddlewareError,
)
from .settings import Settings
from .spider import Spider
from .stats import StatsCollector
from .utils import collect_outputs_with_error, maybe_await


@dataclass(frozen=True, slots=True)
class CrawlResult:
    reason: str
    items: tuple[object, ...]
    stats: dict[str, object]


@dataclass(frozen=True, slots=True)
class _StartFailure:
    exception: BaseException


_START_DONE = object()


class CrawlEngine:
    backend_name = "python"

    def __init__(self, crawler: object, spider: Spider, downloader: Downloader) -> None:
        self.crawler = crawler
        self.spider = spider
        self.downloader = downloader
        self.settings: Settings = crawler.settings  # type: ignore[attr-defined]
        self.signals = crawler.signals  # type: ignore[attr-defined]
        self.stats: StatsCollector = crawler.stats  # type: ignore[attr-defined]
        self.scheduler = EngineScheduler(SchedulerQueueConfig.from_settings(self.settings))
        self.downloader_middleware = DownloaderMiddlewareManager(
            crawler,
            self.settings.get("DOWNLOADER_MIDDLEWARES", {}),
            base=self.settings.get("DOWNLOADER_MIDDLEWARES_BASE", {}),
        )
        self.spider_middleware = SpiderMiddlewareManager(
            crawler,
            self.settings.get("SPIDER_MIDDLEWARES", []),
            base=self.settings.get("SPIDER_MIDDLEWARES_BASE", {}),
        )
        self.item_pipelines = ItemPipelineManager(
            crawler,
            self.settings.get("ITEM_PIPELINES", []),
        )
        self.concurrent_requests = self.settings.getint("CONCURRENT_REQUESTS", 16)
        if self.concurrent_requests < 1:
            raise ValueError("CONCURRENT_REQUESTS must be at least 1")
        self._internal_downloads = asyncio.Semaphore(self.concurrent_requests)
        self.items: list[object] = []

    async def crawl(self) -> CrawlResult:
        tasks: set[asyncio.Task[list[object]]] = set()
        start_producer: asyncio.Task[None] | None = None
        next_start: asyncio.Task[object] | None = None
        spider_opened = False
        reason = "finished"
        try:
            self.stats.set_value("start_time", asyncio.get_running_loop().time())
            await self.signals.send(signals.engine_started)
            spider_opened = True
            await self.signals.send(signals.spider_opened, spider=self.spider)

            start_queue: asyncio.Queue[object] = asyncio.Queue(maxsize=self.concurrent_requests * 2)
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
                    if start_output is _START_DONE:
                        next_start = None
                    elif isinstance(start_output, Request):
                        await self._schedule(start_output)
                    else:
                        await self._process_outputs([start_output])
                    if start_output is not _START_DONE:
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

            await self._finish(reason, spider_opened)
        return CrawlResult(reason, tuple(self.items), dict(self.stats.get_stats()))

    async def _finish(self, reason: str, spider_opened: bool) -> None:
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

    async def _produce_start_requests(
        self,
        queue: asyncio.Queue[object],
    ) -> None:
        try:
            start = self.spider_middleware.process_start(
                self._spider_start_source(),
                self.spider,
            )
            async for output in start:
                if output is None:
                    continue
                if isinstance(output, Request):
                    output.meta.setdefault("is_start_request", True)
                await queue.put(output)
        except BaseException as exception:
            await queue.put(_StartFailure(exception))
        else:
            await queue.put(_START_DONE)

    def _spider_start_source(self) -> object:
        if type(self.spider).start is Spider.start:
            return self.spider.start_requests()
        return self.spider.start()

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
        try:
            downloaded = await self._download(request)
        except CloseSpider:
            raise
        except IgnoreRequest:
            return []
        except Exception as exception:
            return await self._run_errback(request, exception)

        if isinstance(downloaded, Request):
            return [downloaded]
        response = downloaded
        await self.signals.send(
            signals.response_received,
            response=response,
            request=request,
            spider=self.spider,
        )
        return await self._run_callback(request, response)

    async def _download(self, request: Request) -> Request | Response:
        return await self.downloader_middleware.download(request, self.downloader.fetch)

    async def download_async(self, request: Request) -> Response:
        """Download an internal request through downloader middleware."""
        if request.meta.get("_robotstxt_request", False):
            return await self._download_internal(request)
        async with self._internal_downloads:
            return await self._download_internal(request)

    async def _download_internal(self, request: Request) -> Response:
        current = request
        for _ in range(100):
            result = await self._download(current)
            if isinstance(result, Response):
                response = result if result.request is not None else result.replace(request=current)
                await self.signals.send(
                    signals.response_received,
                    response=response,
                    request=current,
                    spider=self.spider,
                )
                return response
            current = result
        raise RuntimeError("download exceeded 100 middleware redirects or retries")

    def download(self, request: Request) -> Awaitable[Response]:
        warnings.warn(
            "CrawlEngine.download() returns an awaitable in SpiderOxide; "
            "use download_async() instead.",
            DeprecationWarning,
            stacklevel=2,
        )
        return self.download_async(request)

    async def _run_callback(self, request: Request, response: Response) -> list[object]:
        callback_exception: Exception | None = None
        try:
            await self.spider_middleware.process_input(response, self.spider)
            callback = request.callback or self.spider._parse
            callback_output = callback(response, **request.cb_kwargs)
            processed, callback_exception = await self._process_spider_stream(
                request,
                response,
                callback_output,
            )
        except CloseSpider:
            raise
        except _InvalidMiddlewareOutput:
            raise
        except Exception as exception:
            processed = []
            callback_exception = exception

        if callback_exception is not None:
            if request.errback is not None:
                try:
                    errback_output = self._call_errback(request, callback_exception)
                    recovered, errback_exception = await self._process_spider_stream(
                        request,
                        response,
                        errback_output,
                    )
                except CloseSpider:
                    raise
                except _InvalidMiddlewareOutput:
                    raise
                except Exception as errback_exception:
                    callback_exception = errback_exception
                else:
                    processed.extend(recovered)
                    if errback_exception is None:
                        return processed
                    callback_exception = errback_exception
            try:
                recovered = await self.spider_middleware.process_exception(
                    response,
                    callback_exception,
                    self.spider,
                )
            except _UnhandledSpiderMiddlewareError as unhandled:
                await self._report_spider_error(
                    request,
                    unhandled.exception,
                    response=response,
                )
                return [*processed, *unhandled.partial]
            if recovered is not None:
                return [*processed, *recovered]
            await self._report_spider_error(
                request,
                callback_exception,
                response=response,
            )
        return processed

    @staticmethod
    def _call_errback(request: Request, exception: Exception) -> object:
        assert request.errback is not None
        if getattr(request.errback, "_spideroxide_request_context", False):
            return request.errback(exception, request=request)
        return request.errback(exception)

    async def _process_spider_stream(
        self,
        request: Request,
        response: Response,
        outputs: object,
    ) -> tuple[list[object], Exception | None]:
        try:
            return await self.spider_middleware.process_output_with_error(
                response,
                outputs,
                self.spider,
            )
        except _UnhandledSpiderMiddlewareError as unhandled:
            await self._report_spider_error(
                request,
                unhandled.exception,
                response=response,
            )
            return unhandled.partial, None

    async def _process_spider_output(
        self,
        request: Request,
        response: Response,
        outputs: list[object],
    ) -> list[object]:
        try:
            return await self.spider_middleware.process_output(
                response,
                outputs,
                self.spider,
            )
        except _UnhandledSpiderMiddlewareError as unhandled:
            await self._report_spider_error(
                request,
                unhandled.exception,
                response=response,
            )
            return unhandled.partial

    async def _run_errback(
        self,
        request: Request,
        exception: Exception,
        *,
        response: Response | None = None,
    ) -> list[object]:
        if request.errback is not None:
            try:
                errback_output = self._call_errback(request, exception)
            except CloseSpider:
                raise
            except Exception as errback_exception:
                exception = errback_exception
                return await self._report_spider_error(
                    request,
                    exception,
                    response=response,
                )
            outputs, errback_exception = await collect_outputs_with_error(errback_output)
            if isinstance(errback_exception, CloseSpider):
                raise errback_exception
            if errback_exception is not None:
                exception = errback_exception
            else:
                return outputs
            await self._report_spider_error(
                request,
                exception,
                response=response,
            )
            return outputs
        return await self._report_spider_error(
            request,
            exception,
            response=response,
        )

    async def _report_spider_error(
        self,
        request: Request,
        exception: Exception,
        *,
        response: Response | None = None,
    ) -> list[object]:
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
            if output is None:
                continue
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


class NativeCrawlEngine(CrawlEngine):
    backend_name = "rust"

    def __init__(self, crawler: object, spider: Spider, downloader: Downloader) -> None:
        settings: Settings = crawler.settings  # type: ignore[attr-defined]
        concurrency = settings.getint("CONCURRENT_REQUESTS", 16)
        if concurrency < 1:
            raise ValueError("CONCURRENT_REQUESTS must be at least 1")
        pending_limit = settings.getint(
            "ENGINE_MAX_PENDING",
            concurrency * 2,
        )
        if pending_limit < 0:
            raise ValueError("ENGINE_MAX_PENDING cannot be negative")
        if pending_limit == 0:
            pending_limit = concurrency * 2
        try:
            from ._native import (
                NativeCrawlCoordinator,
                NativeDepthPolicy,
                NativePolicyRuntime,
                NativeRobotsRuntime,
            )
        except ImportError as error:
            raise BackendUnavailableError(
                "Rust engine requested but the extension is unavailable; "
                "run `maturin develop --release` or select the Python engine"
            ) from error
        from .native_slots import NativeDownloadSlots

        policy_runtime = NativePolicyRuntime()
        depth_policy = NativeDepthPolicy(
            str(settings.getint("DEPTH_LIMIT")),
            str(settings.getint("DEPTH_PRIORITY")),
            settings.getbool("DEPTH_STATS_VERBOSE"),
        )
        robots_runtime = NativeRobotsRuntime()
        configured_job_dir = settings.get("JOBDIR")
        job_dir = (
            os.path.abspath(os.path.expanduser(os.fspath(configured_job_dir)))
            if configured_job_dir
            else None
        )
        queue_config = SchedulerQueueConfig.from_settings(settings)
        coordinator = NativeCrawlCoordinator(
            concurrency,
            pending_limit,
            job_dir,
            queue_config.memory,
            queue_config.disk,
            queue_config.start_memory,
            queue_config.start_disk,
        )
        crawler.native_policy_runtime = policy_runtime
        crawler.native_depth_policy = depth_policy
        crawler.native_robots_runtime = robots_runtime
        try:
            super().__init__(crawler, spider, downloader)
            self.scheduler = coordinator
            self._requests: dict[int, Request] = {}
            self._persistent_request_ids: set[int] = set()
            self._job_dir = job_dir
            self._log_unserializable = settings.getbool("SCHEDULER_DEBUG")
            self._unserializable_logged = False
            recovered = coordinator.take_recovered()
            for request_id, payload in recovered:
                self._requests[request_id] = deserialize_request(payload, spider)
                self._persistent_request_ids.add(request_id)
            if recovered:
                self.stats.inc_value("scheduler/recovered", len(recovered))
                spider.logger.info("Resuming crawl (%d requests scheduled)", len(recovered))
            if job_dir is not None:
                persisted_state = coordinator.load_spider_state()
                spider.state = (
                    deserialize_spider_state(persisted_state) if persisted_state is not None else {}
                )
        except BaseException:
            coordinator.close()
            crawler.native_policy_runtime = None
            crawler.native_depth_policy = None
            crawler.native_download_slots = None
            crawler.native_robots_runtime = None
            raise
        self._native_download_slots_type = NativeDownloadSlots
        self.native_download_slots: NativeDownloadSlots | None = None
        self.native_robots_runtime = robots_runtime

    async def crawl(self) -> CrawlResult:
        tasks: set[asyncio.Task[None]] = set()
        spider_opened = False
        reason = "finished"
        try:
            self.stats.set_value("start_time", asyncio.get_running_loop().time())
            await self.signals.send(signals.engine_started)
            spider_opened = True
            await self.signals.send(signals.spider_opened, spider=self.spider)
            self.native_download_slots = self._native_download_slots_type(
                self.settings,
                self.spider,
                self.stats,
            )
            self.crawler.native_download_slots = self.native_download_slots

            tasks = {asyncio.create_task(self._worker()) for _ in range(self.concurrent_requests)}
            tasks.add(asyncio.create_task(self._produce_native_start_requests()))
            await asyncio.gather(*tasks)
        except CloseSpider as exception:
            reason = exception.reason
        except asyncio.CancelledError:
            reason = "cancelled"
            raise
        except BaseException:
            reason = "error"
            raise
        finally:
            self.scheduler.abort()
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            if self.native_download_slots is not None:
                self.native_download_slots.close()
            from .depth import sync_stats as sync_depth_stats
            from .robots import sync_stats as sync_robots_stats

            sync_depth_stats(self.crawler)
            sync_robots_stats(self.crawler)
            self.native_robots_runtime.close()
            await self._finish(reason, spider_opened)
            if self._job_dir is not None:
                await self._safe_teardown("job/state", self._save_spider_state)
            await self._safe_teardown("job/close", self.scheduler.close)
            self._requests.clear()
            self._persistent_request_ids.clear()
        return CrawlResult(reason, tuple(self.items), dict(self.stats.get_stats()))

    async def _produce_native_start_requests(self) -> None:
        try:
            start = self.spider_middleware.process_start(
                self._spider_start_source(),
                self.spider,
            )
            async for output in start:
                if output is None:
                    continue
                if isinstance(output, Request):
                    output.meta.setdefault("is_start_request", True)
                    if not await self.scheduler.wait_for_pending_slot():
                        return
                    await self._schedule(output)
                else:
                    await self._process_outputs([output])
        finally:
            self.scheduler.close_input()

    async def _worker(self) -> None:
        while (request_id := await self.scheduler.next_request()) is not None:
            try:
                request = self._requests.pop(request_id)
            except KeyError as error:
                raise RuntimeError(
                    f"native coordinator returned unknown request {request_id}"
                ) from error
            self.stats.inc_value("scheduler/dequeued")
            storage = "disk" if request_id in self._persistent_request_ids else "memory"
            self.stats.inc_value(f"scheduler/dequeued/{storage}")
            try:
                outputs = await self._handle_request(request)
                await self._process_outputs(outputs)
            except CloseSpider:
                self.scheduler.release(request_id)
                raise
            except BaseException:
                self.scheduler.release(request_id)
                raise
            else:
                self.scheduler.complete(request_id)
            finally:
                if request_id in self._persistent_request_ids:
                    self._persistent_request_ids.discard(request_id)

    async def _download(self, request: Request) -> Request | Response:
        native_download_slots = self.native_download_slots
        if native_download_slots is None:
            raise RuntimeError("native download slots are not initialized")
        if urlsplit(request.url).hostname is None and request.meta.get("download_slot") is None:
            return await self.downloader_middleware.download(request, self.downloader.fetch)
        return await self.downloader_middleware.download(
            request,
            lambda current: native_download_slots.download(
                current,
                self.downloader.fetch,
            ),
        )

    async def _schedule(self, request: Request) -> bool:
        if not isinstance(request, Request):
            raise TypeError("spider output must contain Request objects or items")
        payload = None
        if self._job_dir is not None:
            try:
                payload = serialize_request(request, self.spider)
            except ValueError as error:
                self.stats.inc_value("scheduler/unserializable")
                if self._log_unserializable and not self._unserializable_logged:
                    self._unserializable_logged = True
                    self.spider.logger.warning(
                        "Unable to serialize request %r; it will not survive resume: %s",
                        request,
                        error,
                    )
        request_id = self.scheduler.schedule(
            request.url,
            request.method,
            request.body,
            str(request.priority),
            not request.dont_filter,
            payload,
            bool(request.meta.get("is_start_request", False)),
        )
        inserted = request_id is not None
        if inserted:
            self._requests[request_id] = request
            self.stats.inc_value("scheduler/enqueued")
            storage = "disk" if self.scheduler.is_persistent(request_id) else "memory"
            self.stats.inc_value(f"scheduler/enqueued/{storage}")
            if storage == "disk":
                self._persistent_request_ids.add(request_id)
            await self.signals.send(
                signals.request_scheduled,
                request=request,
                spider=self.spider,
            )
            self.scheduler.activate(request_id)
        else:
            self.stats.inc_value("dupefilter/filtered")
            await self.signals.send(
                signals.request_dropped,
                request=request,
                spider=self.spider,
            )
        return inserted

    def _save_spider_state(self) -> None:
        state = getattr(self.spider, "state", {})
        self.scheduler.save_spider_state(serialize_spider_state(state))


def create_engine(crawler: object, spider: Spider, downloader: Downloader) -> CrawlEngine:
    settings: Settings = crawler.settings  # type: ignore[attr-defined]
    selected = str(settings.get("ENGINE_BACKEND", "python")).strip().lower()
    if settings.get("JOBDIR") and selected == "python":
        raise ValueError("JOBDIR requires ENGINE_BACKEND='rust' or 'auto'")
    if selected == "python":
        return CrawlEngine(crawler, spider, downloader)
    if selected == "rust":
        return NativeCrawlEngine(crawler, spider, downloader)
    if selected == "auto":
        if settings.get("JOBDIR"):
            return NativeCrawlEngine(crawler, spider, downloader)
        try:
            return NativeCrawlEngine(crawler, spider, downloader)
        except BackendUnavailableError:
            return CrawlEngine(crawler, spider, downloader)
    raise ValueError(f"invalid engine backend {selected!r}; expected 'python', 'rust', or 'auto'")
