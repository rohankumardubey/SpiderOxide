from __future__ import annotations

import asyncio
import logging
import warnings
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, TypedDict

from ..api import fingerprint_request
from ..http import Request, Response
from ..utils import maybe_await

if TYPE_CHECKING:
    from ..crawler import Crawler
    from ..spider import Spider

logger = logging.getLogger(__name__)


class FileInfo(TypedDict):
    url: str
    path: str
    checksum: str | None
    status: str


@dataclass(frozen=True, slots=True)
class _CachedFailure:
    exception_type: type[Exception]
    args: tuple[object, ...]

    @classmethod
    def from_exception(cls, error: Exception) -> _CachedFailure:
        args = error.args
        if not all(
            isinstance(value, (str, int, float, bool, type(None)))
            or (isinstance(value, bytes) and len(value) <= 1024)
            for value in args
        ):
            args = (str(error),)
        return cls(type(error), args)

    def exception(self) -> Exception:
        try:
            return self.exception_type(*self.args)
        except Exception:
            return RuntimeError(
                f"{self.exception_type.__module__}.{self.exception_type.__qualname__}: "
                f"{', '.join(map(str, self.args))}"
            )


class MediaPipeline(ABC):
    LOG_FAILED_RESULTS = True

    class SpiderInfo:
        def __init__(self, spider: Spider) -> None:
            self.spider = spider
            self.downloaded: dict[
                bytes,
                FileInfo | _CachedFailure | asyncio.Task[FileInfo],
            ] = {}

    def __init__(self, download_func: object = None, *, crawler: Crawler) -> None:
        if download_func is not None:
            warnings.warn(
                "The download_func argument is ignored and will be removed in a future release.",
                DeprecationWarning,
                stacklevel=2,
            )
        self.crawler = crawler
        redirect_key = self._key_for_pipe(
            "MEDIA_ALLOW_REDIRECTS",
            base_class_name="MediaPipeline",
        )
        self.allow_redirects = crawler.settings.getbool(redirect_key, False)
        self.spiderinfo: MediaPipeline.SpiderInfo | None = None
        crawler.signals.connect(self.open_spider, "spider_opened")

    @classmethod
    def from_crawler(cls, crawler: Crawler) -> MediaPipeline:
        return cls(crawler=crawler)

    def _key_for_pipe(self, key: str, *, base_class_name: str) -> str:
        class_name = self.__class__.__name__
        custom_key = f"{class_name.upper()}_{key}"
        if class_name == base_class_name or not self.crawler.settings.get(custom_key):
            return key
        return custom_key

    def open_spider(self, spider: Spider) -> None:
        self.spiderinfo = self.SpiderInfo(spider)

    async def process_item(self, item: Any, spider: Spider | None = None) -> Any:
        info = self.spiderinfo
        if info is None:
            active_spider = spider or self.crawler.spider
            if active_spider is None:
                raise RuntimeError("media pipeline requires an active spider")
            info = self.SpiderInfo(active_spider)
            self.spiderinfo = info

        requests = self.get_media_requests(item, info)
        if not isinstance(requests, (list, tuple)):
            requests = list(requests)
        coroutines = [self._process_request(request, info, item) for request in requests]
        gathered = await asyncio.gather(*coroutines, return_exceptions=True)
        results: list[tuple[bool, FileInfo | BaseException]] = [
            (not isinstance(result, BaseException), result) for result in gathered
        ]
        return await maybe_await(self.item_completed(results, item, info))

    async def _process_request(
        self,
        request: Request,
        info: SpiderInfo,
        item: Any,
    ) -> FileInfo:
        if not isinstance(request, Request):
            raise TypeError(
                f"get_media_requests() must return Request objects, got {type(request).__name__}"
            )
        fingerprint = fingerprint_request(request)
        cached = info.downloaded.get(fingerprint)
        if isinstance(cached, Mapping):
            return cached
        if isinstance(cached, _CachedFailure):
            raise cached.exception()
        if cached is None:
            task = asyncio.create_task(self._download_media(request, info, item))
            info.downloaded[fingerprint] = task
        else:
            task = cached
        try:
            result = await task
            info.downloaded[fingerprint] = result
            return result
        except Exception as error:
            failure = _CachedFailure.from_exception(error)
            info.downloaded[fingerprint] = failure
            if request.errback is None:
                raise failure.exception() from None
            if getattr(request.errback, "_spideroxide_request_context", False):
                recovered = request.errback(failure.exception(), request=request)
            else:
                recovered = request.errback(failure.exception())
            value = await maybe_await(recovered)
            if not isinstance(value, Mapping):
                raise TypeError(
                    "media request errback must return a file information mapping"
                ) from error
            return FileInfo(
                url=str(value["url"]),
                path=str(value["path"]),
                checksum=value.get("checksum"),
                status=str(value["status"]),
            )

    async def _download_media(
        self,
        request: Request,
        info: SpiderInfo,
        item: Any,
    ) -> FileInfo:
        existing = await maybe_await(self.media_to_download(request, info, item=item))
        if existing is not None:
            return existing

        meta = dict(request.meta)
        if self.allow_redirects:
            meta.pop("handle_httpstatus_all", None)
            meta.pop("handle_httpstatus_list", None)
        else:
            meta["handle_httpstatus_all"] = True
        download_request = request.replace(callback=None, errback=None, meta=meta)
        engine = self.crawler.engine
        if engine is None:
            raise RuntimeError("crawler engine is not initialized")
        try:
            response = await engine.download_async(download_request)
            return await maybe_await(self.media_downloaded(response, request, info, item=item))
        except Exception as error:
            failed = await maybe_await(self.media_failed(error, request, info))
            if isinstance(failed, Mapping):
                return FileInfo(
                    url=str(failed["url"]),
                    path=str(failed["path"]),
                    checksum=failed.get("checksum"),
                    status=str(failed["status"]),
                )
            raise

    def item_completed(
        self,
        results: list[tuple[bool, FileInfo | BaseException]],
        item: Any,
        info: SpiderInfo,
    ) -> Any | Awaitable[Any]:
        if self.LOG_FAILED_RESULTS:
            for success, value in results:
                if not success:
                    logger.error(
                        "%s found errors processing %r",
                        self.__class__.__name__,
                        item,
                        exc_info=(type(value), value, value.__traceback__),
                        extra={"spider": info.spider},
                    )
        return item

    @abstractmethod
    def get_media_requests(self, item: Any, info: SpiderInfo) -> list[Request]:
        raise NotImplementedError

    @abstractmethod
    def media_to_download(
        self,
        request: Request,
        info: SpiderInfo,
        *,
        item: Any = None,
    ) -> FileInfo | None | Awaitable[FileInfo | None]:
        raise NotImplementedError

    @abstractmethod
    def media_downloaded(
        self,
        response: Response,
        request: Request,
        info: SpiderInfo,
        *,
        item: Any = None,
    ) -> FileInfo | Awaitable[FileInfo]:
        raise NotImplementedError

    @abstractmethod
    def media_failed(
        self,
        error: Exception,
        request: Request,
        info: SpiderInfo,
    ) -> object:
        raise NotImplementedError

    @abstractmethod
    def file_path(
        self,
        request: Request,
        response: Response | None = None,
        info: SpiderInfo | None = None,
        *,
        item: Any = None,
    ) -> str:
        raise NotImplementedError
