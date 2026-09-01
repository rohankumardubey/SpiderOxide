from __future__ import annotations

import inspect
from collections.abc import AsyncIterable, AsyncIterator, Awaitable, Callable, Iterable

from .components import build_components
from .exceptions import CloseSpider, DropItem
from .http import Request, Response
from .utils import _is_output_collection, maybe_await

DownloadCallable = Callable[[Request], Awaitable[Response]]


class _InvalidMiddlewareOutput(TypeError):
    pass


class _UnhandledSpiderMiddlewareError(Exception):
    def __init__(
        self,
        exception: Exception,
        partial: list[object] | None = None,
    ) -> None:
        self.exception = exception
        self.partial = partial or []
        super().__init__(str(exception))


class _SpiderOutputSourceError(Exception):
    def __init__(self, exception: Exception) -> None:
        self.exception = exception
        super().__init__(str(exception))


class _OutputMiddlewareError(Exception):
    def __init__(self, exception: Exception, index: int) -> None:
        self.exception = exception
        self.index = index
        super().__init__(str(exception))


def _call_spider_method(
    method: Callable[..., object],
    *args: object,
    spider: object,
) -> object:
    parameters = inspect.signature(method).parameters
    if "spider" in parameters:
        return method(*args, spider=spider)
    return method(*args)


async def _iterate_middleware_output(
    value: object,
    method_name: str,
) -> AsyncIterator[object]:
    value = await maybe_await(value)
    if isinstance(value, AsyncIterable):
        async for item in value:
            yield item
        return
    if _is_output_collection(value):
        for item in value:
            yield item
        return
    raise _InvalidMiddlewareOutput(f"{method_name} must return an iterable")


async def _as_async_iterable(items: Iterable[object]) -> AsyncIterator[object]:
    for item in items:
        yield item


async def _iterate_spider_output(value: object) -> AsyncIterator[object]:
    value = await maybe_await(value)
    if value is None:
        return
    if isinstance(value, AsyncIterable):
        async for item in value:
            yield item
        return
    if _is_output_collection(value):
        for item in value:
            yield item
        return
    yield value


class DownloaderMiddlewareManager:
    def __init__(self, crawler: object, middleware: object, *, base: object = None) -> None:
        self.crawler = crawler
        self.middleware = build_components(middleware, crawler, base=base)

    async def download(self, request: Request, download: DownloadCallable) -> Request | Response:
        spider = self.crawler.spider  # type: ignore[attr-defined]
        response: Request | Response | None = None
        try:
            for component in self.middleware:
                method = getattr(component, "process_request", None)
                if method is None:
                    continue
                result = await maybe_await(method(request, spider))
                if result is not None:
                    if not isinstance(result, (Request, Response)):
                        raise TypeError("process_request must return Request, Response, or None")
                    response = result
                    break
            if response is None:
                response = await download(request)
        except Exception as exception:
            response = await self._process_exception(request, exception, spider)
            if response is None:
                raise

        if isinstance(response, Request):
            return response
        for component in reversed(self.middleware):
            method = getattr(component, "process_response", None)
            if method is None:
                continue
            result = await maybe_await(method(request, response, spider))
            if not isinstance(result, (Request, Response)):
                raise TypeError("process_response must return Request or Response")
            response = result
            if isinstance(response, Request):
                break
        return response

    async def _process_exception(
        self,
        request: Request,
        exception: Exception,
        spider: object,
    ) -> Request | Response | None:
        for component in reversed(self.middleware):
            method = getattr(component, "process_exception", None)
            if method is None:
                continue
            result = await maybe_await(method(request, exception, spider))
            if result is not None:
                if not isinstance(result, (Request, Response)):
                    raise TypeError("process_exception must return Request, Response, or None")
                return result
        return None


class SpiderMiddlewareManager:
    def __init__(self, crawler: object, middleware: object, *, base: object = None) -> None:
        self.middleware = build_components(middleware, crawler, base=base)

    async def process_input(self, response: Response, spider: object) -> None:
        for component in self.middleware:
            method = getattr(component, "process_spider_input", None)
            if method is None:
                continue
            result = await maybe_await(_call_spider_method(method, response, spider=spider))
            if result is not None:
                raise _InvalidMiddlewareOutput("process_spider_input must return None")

    async def process_output(
        self,
        response: Response,
        outputs: object,
        spider: object,
        *,
        start_index: int = 0,
    ) -> list[object]:
        processed, source_error = await self.process_output_with_error(
            response,
            outputs,
            spider,
            start_index=start_index,
        )
        if source_error is not None:
            raise source_error
        return processed

    async def process_output_with_error(
        self,
        response: Response,
        outputs: object,
        spider: object,
        *,
        start_index: int = 0,
        strict_source: str | None = None,
    ) -> tuple[list[object], Exception | None]:
        source = self._output_source(outputs, strict_source)
        chain = list(reversed(self.middleware))
        for index in range(start_index, len(chain)):
            component = chain[index]
            async_method = getattr(component, "process_spider_output_async", None)
            method = async_method or getattr(component, "process_spider_output", None)
            if method is None:
                continue
            source = self._wrap_output_method(
                source,
                response,
                spider,
                method,
                index,
                asynchronous=(async_method is not None or inspect.isasyncgenfunction(method)),
            )

        processed: list[object] = []
        try:
            async for output in source:
                processed.append(output)
        except _SpiderOutputSourceError as error:
            return processed, error.exception
        except _OutputMiddlewareError as error:
            try:
                recovered = await self._process_exception_from(
                    response,
                    error.exception,
                    spider,
                    start_index=error.index + 1,
                )
            except _UnhandledSpiderMiddlewareError as unhandled:
                raise _UnhandledSpiderMiddlewareError(
                    unhandled.exception,
                    [*processed, *unhandled.partial],
                ) from unhandled.exception
            if recovered is None:
                raise _UnhandledSpiderMiddlewareError(
                    error.exception,
                    processed,
                ) from error.exception
            processed.extend(recovered)
        return processed, None

    def _output_source(
        self,
        outputs: object,
        strict_source: str | None,
    ) -> AsyncIterator[object]:
        async def iterate() -> AsyncIterator[object]:
            try:
                iterator = (
                    _iterate_middleware_output(outputs, strict_source)
                    if strict_source is not None
                    else _iterate_spider_output(outputs)
                )
                async for output in iterator:
                    yield output
            except (CloseSpider, _InvalidMiddlewareOutput):
                raise
            except Exception as exception:
                raise _SpiderOutputSourceError(exception) from exception

        return iterate()

    def _wrap_output_method(
        self,
        source: AsyncIterable[object],
        response: Response,
        spider: object,
        method: Callable[..., object],
        index: int,
        *,
        asynchronous: bool,
    ) -> AsyncIterator[object]:
        async def iterate() -> AsyncIterator[object]:
            pending_error: list[Exception] = []
            try:
                source_error: _SpiderOutputSourceError | _OutputMiddlewareError | None = None
                if asynchronous:

                    async def guarded_source() -> AsyncIterator[object]:
                        try:
                            async for output in source:
                                yield output
                        except (
                            CloseSpider,
                            _InvalidMiddlewareOutput,
                            _SpiderOutputSourceError,
                            _OutputMiddlewareError,
                        ) as error:
                            pending_error.append(error)

                    method_input: object = guarded_source()
                else:
                    buffered = []
                    try:
                        async for output in source:
                            buffered.append(output)
                    except (_SpiderOutputSourceError, _OutputMiddlewareError) as error:
                        source_error = error
                    method_input = iter(buffered)
                result = _call_spider_method(
                    method,
                    response,
                    method_input,
                    spider=spider,
                )
                async for output in _iterate_middleware_output(
                    result,
                    "process_spider_output",
                ):
                    yield output
                if asynchronous and pending_error:
                    raise pending_error[0]
                if source_error is not None:
                    raise source_error
            except (
                CloseSpider,
                _InvalidMiddlewareOutput,
                _SpiderOutputSourceError,
                _OutputMiddlewareError,
            ):
                raise
            except Exception as exception:
                if pending_error:
                    raise pending_error[0] from exception
                raise _OutputMiddlewareError(exception, index) from exception

        return iterate()

    async def process_exception(
        self,
        response: Response,
        exception: Exception,
        spider: object,
    ) -> list[object] | None:
        if isinstance(exception, _InvalidMiddlewareOutput):
            raise exception
        return await self._process_exception_from(
            response,
            exception,
            spider,
            start_index=0,
        )

    async def _process_exception_from(
        self,
        response: Response,
        exception: Exception,
        spider: object,
        *,
        start_index: int,
    ) -> list[object] | None:
        chain = list(reversed(self.middleware))
        handler_failed = False
        for index in range(start_index, len(chain)):
            component = chain[index]
            method = getattr(component, "process_spider_exception", None)
            if method is None:
                continue
            try:
                result = await maybe_await(
                    _call_spider_method(
                        method,
                        response,
                        exception,
                        spider=spider,
                    )
                )
            except (CloseSpider, _InvalidMiddlewareOutput):
                raise
            except Exception as handler_exception:
                exception = handler_exception
                handler_failed = True
                continue
            if result is not None:
                recovered, recovery_error = await self.process_output_with_error(
                    response,
                    result,
                    spider,
                    start_index=index + 1,
                    strict_source="process_spider_exception",
                )
                if recovery_error is not None:
                    try:
                        downstream = await self._process_exception_from(
                            response,
                            recovery_error,
                            spider,
                            start_index=index + 1,
                        )
                    except _UnhandledSpiderMiddlewareError as unhandled:
                        raise _UnhandledSpiderMiddlewareError(
                            unhandled.exception,
                            [*recovered, *unhandled.partial],
                        ) from unhandled.exception
                    if downstream is None:
                        raise _UnhandledSpiderMiddlewareError(
                            recovery_error,
                            recovered,
                        ) from recovery_error
                    return [*recovered, *downstream]
                return recovered
        if handler_failed:
            raise _UnhandledSpiderMiddlewareError(exception) from exception
        return None

    async def process_start(
        self,
        start: AsyncIterable[object] | Iterable[object],
        spider: object,
    ) -> AsyncIterator[object]:
        current = await maybe_await(start)
        self._validate_start_output(current, "start")
        modern_seen = False
        for component in reversed(self.middleware):
            method = getattr(component, "process_start", None)
            if method is not None:
                if not isinstance(current, AsyncIterable):
                    assert isinstance(current, Iterable)
                    current = _as_async_iterable(current)
                current = await maybe_await(_call_spider_method(method, current, spider=spider))
                self._validate_start_output(current, "process_start")
                modern_seen = True
                continue
            legacy = getattr(component, "process_start_requests", None)
            if legacy is None:
                continue
            if modern_seen or isinstance(current, AsyncIterable):
                raise TypeError(
                    "process_start_requests cannot consume asynchronous start output; "
                    "implement process_start instead"
                )
            current = await maybe_await(legacy(current, spider))
            self._validate_start_output(current, "process_start_requests")
        async for item in _iterate_middleware_output(current, "process_start"):
            yield item

    @staticmethod
    def _validate_start_output(value: object, method_name: str) -> None:
        if isinstance(value, AsyncIterable):
            return
        if _is_output_collection(value):
            return
        raise _InvalidMiddlewareOutput(f"{method_name} must return an iterable")


class ItemPipelineManager:
    def __init__(self, crawler: object, pipelines: object) -> None:
        self.pipelines = build_components(pipelines, crawler)

    async def process_item(self, item: object, spider: object) -> object:
        current = item
        for pipeline in self.pipelines:
            method = getattr(pipeline, "process_item", None)
            if method is None:
                raise TypeError(f"{type(pipeline).__name__} has no process_item method")
            current = await maybe_await(method(current, spider))
            if current is None:
                raise DropItem(f"{type(pipeline).__name__} returned None")
        return current
