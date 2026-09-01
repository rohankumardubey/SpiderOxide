from __future__ import annotations

import functools
import hashlib
import logging
import mimetypes
import time
from collections.abc import Awaitable, Mapping
from contextlib import suppress
from io import BytesIO
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar
from urllib.parse import urlparse

from itemadapter import ItemAdapter

from ..exceptions import NotConfigured
from ..http import Request, Response
from ..utils import maybe_await
from .media import FileInfo, MediaPipeline

if TYPE_CHECKING:
    from os import PathLike

    from ..crawler import Crawler

logger = logging.getLogger(__name__)


class FileException(Exception):
    """General media processing error."""


class FSFilesStore:
    def __init__(self, basedir: str | PathLike[str]) -> None:
        root = str(basedir)
        if "://" in root:
            root = root.split("://", 1)[1]
        from .._native import NativeMediaStore

        self.basedir = root
        self._store = NativeMediaStore(Path(root))

    def persist_file(
        self,
        path: str,
        buf: BytesIO,
        info: MediaPipeline.SpiderInfo,
        meta: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> str:
        del info, meta, headers
        return self._store.persist(path, buf.getvalue())

    def stat_file(self, path: str, info: MediaPipeline.SpiderInfo) -> dict[str, object]:
        del info
        result = self._store.stat(path)
        if result is None:
            return {}
        last_modified, checksum = result
        return {"last_modified": last_modified, "checksum": checksum}


class FilesPipeline(MediaPipeline):
    MEDIA_NAME = "file"
    EXPIRES = 90
    STORE_SCHEMES: ClassVar[dict[str, type[FSFilesStore]]] = {
        "": FSFilesStore,
        "file": FSFilesStore,
    }
    DEFAULT_FILES_URLS_FIELD = "file_urls"
    DEFAULT_FILES_RESULT_FIELD = "files"

    def __init__(
        self,
        store_uri: str | PathLike[str],
        download_func: object = None,
        *,
        crawler: Crawler,
    ) -> None:
        if not store_uri:
            setting_name = (
                "IMAGES_STORE" if self.__class__.__name__ == "ImagesPipeline" else "FILES_STORE"
            )
            raise NotConfigured(
                f"{setting_name} setting must be set to a valid path (not empty) "
                f"to enable {self.__class__.__name__}."
            )
        self.store = self._get_store(str(store_uri))
        super().__init__(download_func, crawler=crawler)
        resolve = functools.partial(self._key_for_pipe, base_class_name="FilesPipeline")
        self.expires = crawler.settings.getint(resolve("FILES_EXPIRES"), self.EXPIRES)
        urls_field = getattr(self, "FILES_URLS_FIELD", self.DEFAULT_FILES_URLS_FIELD)
        result_field = getattr(self, "FILES_RESULT_FIELD", self.DEFAULT_FILES_RESULT_FIELD)
        self.files_urls_field = str(crawler.settings.get(resolve("FILES_URLS_FIELD"), urls_field))
        self.files_result_field = str(
            crawler.settings.get(resolve("FILES_RESULT_FIELD"), result_field)
        )

    @classmethod
    def from_crawler(cls, crawler: Crawler) -> FilesPipeline:
        return cls(crawler.settings.get("FILES_STORE"), crawler=crawler)

    def _get_store(self, uri: str) -> FSFilesStore:
        scheme = "file" if Path(uri).is_absolute() else urlparse(uri).scheme
        try:
            store_class = self.STORE_SCHEMES[scheme]
        except KeyError:
            supported = ", ".join(repr(value or "local path") for value in self.STORE_SCHEMES)
            raise NotConfigured(
                f"unsupported media store scheme {scheme!r}; supported schemes: {supported}"
            ) from None
        return store_class(uri)

    async def media_to_download(
        self,
        request: Request,
        info: MediaPipeline.SpiderInfo,
        *,
        item: Any = None,
    ) -> FileInfo | None:
        path = self.file_path(request, info=info, item=item)
        try:
            result = await maybe_await(self.store.stat_file(path, info))
        except Exception:
            logger.exception(
                "%s.store.stat_file",
                self.__class__.__name__,
                extra={"spider": info.spider},
            )
            return None
        if not result or not isinstance(result, Mapping):
            return None
        modified = result.get("last_modified")
        if not isinstance(modified, (int, float)):
            return None
        if (time.time() - modified) / 86400 > self.expires:
            return None
        self.inc_stats("uptodate")
        checksum = result.get("checksum")
        return {
            "url": request.url,
            "path": path,
            "checksum": str(checksum) if checksum is not None else None,
            "status": "uptodate",
        }

    async def media_downloaded(
        self,
        response: Response,
        request: Request,
        info: MediaPipeline.SpiderInfo,
        *,
        item: Any = None,
    ) -> FileInfo:
        if response.status != 200:
            raise FileException("download-error")
        if not response.body:
            raise FileException("empty-content")
        status = "cached" if "cached" in response.flags else "downloaded"
        self.inc_stats(status)
        try:
            path = self.file_path(request, response=response, info=info, item=item)
            checksum = await maybe_await(self.file_downloaded(response, request, info, item=item))
        except FileException:
            raise
        except Exception as error:
            raise FileException(str(error)) from error
        return {
            "url": request.url,
            "path": path,
            "checksum": checksum,
            "status": status,
        }

    def media_failed(
        self,
        error: Exception,
        request: Request,
        info: MediaPipeline.SpiderInfo,
    ) -> object:
        logger.warning(
            "Error downloading %s from %s: %s",
            self.MEDIA_NAME,
            request.url,
            error,
            extra={"spider": info.spider},
        )
        raise FileException from error

    def inc_stats(self, status: str) -> None:
        self.crawler.stats.inc_value("file_count")
        self.crawler.stats.inc_value(f"file_status_count/{status}")

    def get_media_requests(
        self,
        item: Any,
        info: MediaPipeline.SpiderInfo,
    ) -> list[Request]:
        del info
        urls = ItemAdapter(item).get(self.files_urls_field, [])
        if not isinstance(urls, list):
            raise TypeError(
                f"{self.files_urls_field} must be a list of URLs, got {type(urls).__name__}."
            )
        return [Request(url) for url in urls]

    async def file_downloaded(
        self,
        response: Response,
        request: Request,
        info: MediaPipeline.SpiderInfo,
        *,
        item: Any = None,
    ) -> str:
        path = self.file_path(request, response=response, info=info, item=item)
        content = BytesIO(response.body)
        persisted = await maybe_await(self.store.persist_file(path, content, info))
        if isinstance(persisted, str):
            return persisted
        return hashlib.md5(response.body).hexdigest()  # noqa: S324

    def item_completed(
        self,
        results: list[tuple[bool, FileInfo | BaseException]],
        item: Any,
        info: MediaPipeline.SpiderInfo,
    ) -> Any | Awaitable[Any]:
        super().item_completed(results, item, info)
        with suppress(KeyError):
            ItemAdapter(item)[self.files_result_field] = [
                value for success, value in results if success
            ]
        return item

    def file_path(
        self,
        request: Request,
        response: Response | None = None,
        info: MediaPipeline.SpiderInfo | None = None,
        *,
        item: Any = None,
    ) -> str:
        del response, info, item
        media_guid = hashlib.sha1(request.url.encode()).hexdigest()  # noqa: S324
        parsed = urlparse(request.url)
        media_extension = Path(parsed.path).suffix
        if media_extension not in mimetypes.types_map:
            media_extension = Path(request.url).suffix
        if media_extension not in mimetypes.types_map:
            media_type = mimetypes.guess_type(request.url)[0]
            media_extension = mimetypes.guess_extension(media_type) if media_type else ""
        return f"full/{media_guid}{media_extension or ''}"
