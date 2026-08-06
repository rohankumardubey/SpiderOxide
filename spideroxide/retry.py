from __future__ import annotations

import logging
from http.client import responses
from typing import TYPE_CHECKING

from .components import load_object
from .exceptions import NotConfigured
from .http import Request, Response
from .settings import Settings

if TYPE_CHECKING:
    from .crawler import Crawler
    from .spider import Spider

retry_logger = logging.getLogger(__name__)


def _reason_name(reason: str | Exception | type[Exception]) -> str:
    if isinstance(reason, str):
        return reason
    exception_type = reason if isinstance(reason, type) else type(reason)
    return f"{exception_type.__module__}.{exception_type.__qualname__}"


def _log_level(level: int | str) -> int:
    if isinstance(level, int):
        return level
    resolved = logging.getLevelName(level.upper())
    if not isinstance(resolved, int):
        raise ValueError(f"invalid retry give-up log level: {level!r}")
    return resolved


def get_retry_request(
    request: Request,
    *,
    spider: Spider,
    reason: str | Exception | type[Exception] = "unspecified",
    max_retry_times: int | None = None,
    priority_adjust: int | None = None,
    logger: logging.Logger = retry_logger,
    give_up_log_level: int | str | None = None,
    stats_base_key: str = "retry",
) -> Request | None:
    crawler = spider.crawler
    if crawler is None:
        raise RuntimeError("spider must be bound to a crawler before retrying requests")

    retry_times = int(request.meta.get("retry_times", 0)) + 1
    if max_retry_times is None:
        configured = request.meta.get("max_retry_times")
        max_retry_times = (
            crawler.settings.getint("RETRY_TIMES") if configured is None else int(configured)
        )
    if max_retry_times < 0:
        raise ValueError("max_retry_times cannot be negative")

    reason_name = _reason_name(reason)
    if retry_times <= max_retry_times:
        if priority_adjust is None:
            configured_priority = request.meta.get("priority_adjust")
            priority_adjust = (
                crawler.settings.getint("RETRY_PRIORITY_ADJUST")
                if configured_priority is None
                else int(configured_priority)
            )
        retry_meta = dict(request.meta)
        retry_meta["retry_times"] = retry_times
        retry_request = request.replace(
            meta=retry_meta,
            dont_filter=True,
            priority=request.priority + priority_adjust,
        )
        crawler.stats.inc_value(f"{stats_base_key}/count")
        crawler.stats.inc_value(f"{stats_base_key}/reason_count/{reason_name}")
        logger.debug(
            "Retrying %s (failed %d times): %s",
            request,
            retry_times,
            reason,
            extra={"spider": spider},
        )
        return retry_request

    if give_up_log_level is None:
        give_up_log_level = request.meta.get("give_up_log_level")
        if give_up_log_level is None:
            give_up_log_level = crawler.settings.get("RETRY_GIVE_UP_LOG_LEVEL", "ERROR")
    crawler.stats.inc_value(f"{stats_base_key}/max_reached")
    logger.log(
        _log_level(give_up_log_level),
        "Gave up retrying %s (failed %d times): %s",
        request,
        retry_times,
        reason,
        extra={"spider": spider},
    )
    return None


class RetryMiddleware:
    def __init__(self, settings: Settings) -> None:
        if not settings.getbool("RETRY_ENABLED"):
            raise NotConfigured
        self.max_retry_times = settings.getint("RETRY_TIMES")
        if self.max_retry_times < 0:
            raise ValueError("RETRY_TIMES cannot be negative")
        self.retry_http_codes = {int(status) for status in settings.getlist("RETRY_HTTP_CODES")}
        self.priority_adjust = settings.getint("RETRY_PRIORITY_ADJUST")
        self.give_up_log_level = settings.get("RETRY_GIVE_UP_LOG_LEVEL", "ERROR")
        self.exceptions_to_retry = self._exception_types(settings.getlist("RETRY_EXCEPTIONS"))

    @staticmethod
    def _exception_types(references: list[object]) -> tuple[type[Exception], ...]:
        exception_types = []
        for reference in references:
            candidate = load_object(reference) if isinstance(reference, str) else reference
            if not isinstance(candidate, type) or not issubclass(candidate, Exception):
                raise TypeError("RETRY_EXCEPTIONS entries must be exception classes")
            exception_types.append(candidate)
        return tuple(exception_types)

    @classmethod
    def from_crawler(cls, crawler: Crawler) -> RetryMiddleware:
        middleware = cls(crawler.settings)
        middleware.crawler = crawler
        return middleware

    def process_response(
        self,
        request: Request,
        response: Response,
        spider: Spider,
    ) -> Request | Response:
        if request.meta.get("dont_retry", False):
            return response
        if response.status in self.retry_http_codes:
            reason = f"{response.status} {responses.get(response.status, 'Unknown Status')}"
            return self._retry(request, reason, spider) or response
        return response

    def process_exception(
        self,
        request: Request,
        exception: Exception,
        spider: Spider,
    ) -> Request | Response | None:
        if request.meta.get("dont_retry", False):
            return None
        if isinstance(exception, self.exceptions_to_retry):
            return self._retry(request, exception, spider)
        return None

    def _retry(
        self,
        request: Request,
        reason: str | Exception | type[Exception],
        spider: Spider,
    ) -> Request | None:
        max_retry_override = request.meta.get("max_retry_times")
        max_retry_times = (
            self.max_retry_times if max_retry_override is None else int(max_retry_override)
        )
        priority_override = request.meta.get("priority_adjust")
        priority_adjust = (
            self.priority_adjust if priority_override is None else int(priority_override)
        )
        give_up_log_level = request.meta.get("give_up_log_level")
        if give_up_log_level is None:
            give_up_log_level = self.give_up_log_level
        return get_retry_request(
            request,
            reason=reason,
            spider=spider,
            max_retry_times=max_retry_times,
            priority_adjust=priority_adjust,
            give_up_log_level=give_up_log_level,
        )
