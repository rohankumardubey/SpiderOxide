from __future__ import annotations

from collections.abc import Awaitable, Callable

from .components import build_components
from .exceptions import DropItem
from .http import Request, Response
from .utils import collect_outputs, maybe_await

DownloadCallable = Callable[[Request], Awaitable[Response]]


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
            result = await maybe_await(method(response, spider))
            if result is not None:
                raise TypeError("process_spider_input must return None")

    async def process_output(
        self,
        response: Response,
        outputs: list[object],
        spider: object,
    ) -> list[object]:
        current = outputs
        for component in reversed(self.middleware):
            method = getattr(component, "process_spider_output", None)
            if method is None:
                continue
            current = await collect_outputs(method(response, current, spider))
        return current

    async def process_exception(
        self,
        response: Response,
        exception: Exception,
        spider: object,
    ) -> list[object] | None:
        for component in reversed(self.middleware):
            method = getattr(component, "process_spider_exception", None)
            if method is None:
                continue
            result = await maybe_await(method(response, exception, spider))
            if result is not None:
                return await collect_outputs(result)
        return None


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
