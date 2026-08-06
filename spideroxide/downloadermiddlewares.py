from __future__ import annotations

from typing import TYPE_CHECKING

from .exceptions import NotConfigured
from .http import Request, Response
from .native_policy import runtime_for, sync_stats
from .stats import StatsCollector

if TYPE_CHECKING:
    from .crawler import Crawler
    from .spider import Spider


class DownloaderStatsMiddleware:
    def __init__(self, stats: StatsCollector) -> None:
        self.stats = stats
        self.crawler: Crawler | None = None

    @classmethod
    def from_crawler(cls, crawler: Crawler) -> DownloaderStatsMiddleware:
        if not crawler.settings.getbool("DOWNLOADER_STATS"):
            raise NotConfigured
        middleware = cls(crawler.stats)
        middleware.crawler = crawler
        return middleware

    def process_request(self, request: Request, spider: Spider) -> None:
        runtime = runtime_for(self.crawler)
        if runtime is not None:
            runtime.record_request(request.method)
            assert self.crawler is not None
            sync_stats(self.crawler)
            return None
        self.stats.inc_value("downloader/request_count")
        self.stats.inc_value(f"downloader/request_method_count/{request.method}")
        return None

    def process_response(
        self,
        request: Request,
        response: Response,
        spider: Spider,
    ) -> Response:
        runtime = runtime_for(self.crawler)
        if runtime is not None:
            runtime.record_response(response.status)
            assert self.crawler is not None
            sync_stats(self.crawler)
            return response
        self.stats.inc_value("downloader/response_count")
        self.stats.inc_value(f"downloader/response_status_count/{response.status}")
        return response

    def process_exception(
        self,
        request: Request,
        exception: Exception,
        spider: Spider,
    ) -> None:
        exception_type = f"{type(exception).__module__}.{type(exception).__qualname__}"
        runtime = runtime_for(self.crawler)
        if runtime is not None:
            runtime.record_exception(exception_type)
            assert self.crawler is not None
            sync_stats(self.crawler)
            return None
        self.stats.inc_value("downloader/exception_count")
        self.stats.inc_value(f"downloader/exception_type_count/{exception_type}")
        return None
