from __future__ import annotations

import time
from email.utils import formatdate, mktime_tz, parsedate_tz
from pathlib import Path
from typing import TYPE_CHECKING

from .api import fingerprint_request
from .components import load_object
from .downloader import _response_type
from .exceptions import DownloadError, IgnoreRequest, NotConfigured
from .headers import Headers
from .http import Request, Response
from .signals import spider_closed, spider_opened

if TYPE_CHECKING:
    from .crawler import Crawler
    from .settings import Settings
    from .spider import Spider


def _cache_control(headers: Headers) -> dict[str, str | None]:
    directives: dict[str, str | None] = {}
    for value in headers.getlist("Cache-Control"):
        for entry in value.decode("latin-1").split(","):
            name, separator, raw_value = entry.strip().partition("=")
            if name:
                directives[name.lower()] = raw_value.strip().strip('"') if separator else None
    return directives


def _http_timestamp(value: bytes | None) -> float | None:
    if value is None:
        return None
    try:
        parsed = parsedate_tz(value.decode("ascii"))
        return float(mktime_tz(parsed))  # type: ignore[arg-type]
    except (TypeError, UnicodeDecodeError, ValueError, OverflowError):
        return None


class NativeHttpCacheStorage:
    def __init__(self, settings: Settings) -> None:
        self.directory = Path(str(settings.get("HTTPCACHE_DIR", "httpcache"))).expanduser()
        self.expiration_secs = settings.getint("HTTPCACHE_EXPIRATION_SECS", 0)
        if self.expiration_secs < 0:
            raise ValueError("HTTPCACHE_EXPIRATION_SECS cannot be negative")
        self.store: object | None = None

    def open_spider(self, spider: Spider) -> None:
        from ._native import NativeHttpCacheStore

        self.store = NativeHttpCacheStore(str(self.directory / spider.name))

    def _store(self) -> object:
        if self.store is None:
            raise RuntimeError("HTTP cache storage is not open")
        return self.store

    def retrieve_response(self, spider: Spider, request: Request) -> Response | None:
        cached = self._store().retrieve(  # type: ignore[attr-defined]
            fingerprint_request(request, backend="rust"),
            time.time(),
            self.expiration_secs,
        )
        if cached is None:
            return None
        _, url, status, header_pairs, body = cached
        headers = Headers(header_pairs)
        response_type = _response_type(headers, url=url, body=body)
        return response_type(
            url=url,
            status=status,
            headers=headers,
            body=body,
            request=request,
            flags=("cached",),
        )

    def store_response(
        self,
        spider: Spider,
        request: Request,
        response: Response,
    ) -> None:
        self._store().store(  # type: ignore[attr-defined]
            fingerprint_request(request, backend="rust"),
            time.time(),
            response.url,
            response.status,
            response.headers.to_raw_pairs(),
            response.body,
        )

    def close_spider(self, spider: Spider) -> None:
        if self.store is not None:
            self._store().close()  # type: ignore[attr-defined]
            self.store = None


class DummyPolicy:
    def __init__(self, settings: Settings) -> None:
        self.ignore_schemes = {
            str(value).lower() for value in settings.getlist("HTTPCACHE_IGNORE_SCHEMES", ["file"])
        }
        self.ignore_http_codes = {
            int(value) for value in settings.getlist("HTTPCACHE_IGNORE_HTTP_CODES")
        }

    def should_cache_request(self, request: Request) -> bool:
        return request.url.partition(":")[0].lower() not in self.ignore_schemes

    def should_cache_response(self, response: Response, request: Request) -> bool:
        return response.status not in self.ignore_http_codes

    def is_cached_response_fresh(
        self,
        cached_response: Response,
        request: Request,
    ) -> bool:
        return True

    def is_cached_response_valid(
        self,
        cached_response: Response,
        response: Response,
        request: Request,
    ) -> bool:
        return True


class RFC2616Policy(DummyPolicy):
    MAXAGE = 365 * 24 * 60 * 60

    def __init__(self, settings: Settings) -> None:
        super().__init__(settings)
        self.always_store = settings.getbool("HTTPCACHE_ALWAYS_STORE")
        self.ignore_response_cache_controls = {
            str(value).lower()
            for value in settings.getlist("HTTPCACHE_IGNORE_RESPONSE_CACHE_CONTROLS")
        }

    def _response_cache_control(self, response: Response) -> dict[str, str | None]:
        directives = _cache_control(response.headers)
        for name in self.ignore_response_cache_controls:
            directives.pop(name, None)
        return directives

    def should_cache_request(self, request: Request) -> bool:
        if not super().should_cache_request(request):
            return False
        return "no-store" not in _cache_control(request.headers)

    def should_cache_response(self, response: Response, request: Request) -> bool:
        response_cc = self._response_cache_control(response)
        if "no-store" in response_cc or response.status == 304:
            return False
        if self.always_store:
            return True
        if "max-age" in response_cc or response.headers.get("Expires") is not None:
            return True
        if response.status in {300, 301, 308}:
            return True
        return response.status in {200, 203, 401} and (
            response.headers.get("Last-Modified") is not None
            or response.headers.get("ETag") is not None
        )

    def is_cached_response_fresh(
        self,
        cached_response: Response,
        request: Request,
    ) -> bool:
        request_cc = _cache_control(request.headers)
        response_cc = self._response_cache_control(cached_response)
        if "no-cache" in request_cc or "no-cache" in response_cc:
            return self._set_conditional_headers(cached_response, request)
        now = time.time()
        freshness = self._freshness_lifetime(cached_response, response_cc, now)
        request_max_age = self._seconds(request_cc.get("max-age"))
        if request_max_age is not None:
            freshness = min(freshness, request_max_age)
        date = _http_timestamp(cached_response.headers.get("Date")) or now
        age = max(0.0, now - date)
        header_age = self._seconds(cached_response.headers.get("Age"))
        if header_age is not None:
            age = max(age, header_age)
        if age < freshness:
            return True
        if "max-stale" in request_cc and "must-revalidate" not in response_cc:
            stale = request_cc["max-stale"]
            if stale is None:
                return True
            stale_seconds = self._seconds(stale)
            if stale_seconds is not None and age < freshness + stale_seconds:
                return True
        return self._set_conditional_headers(cached_response, request)

    def is_cached_response_valid(
        self,
        cached_response: Response,
        response: Response,
        request: Request,
    ) -> bool:
        if response.status == 304:
            return True
        return response.status >= 500 and (
            "must-revalidate" not in self._response_cache_control(cached_response)
        )

    def _freshness_lifetime(
        self,
        response: Response,
        cache_control: dict[str, str | None],
        now: float,
    ) -> float:
        max_age = self._seconds(cache_control.get("max-age"))
        if max_age is not None:
            return max_age
        date = _http_timestamp(response.headers.get("Date")) or now
        if response.headers.get("Expires") is not None:
            expires = _http_timestamp(response.headers.get("Expires"))
            return max(0.0, expires - date) if expires is not None else 0.0
        modified = _http_timestamp(response.headers.get("Last-Modified"))
        if modified is not None and modified <= date:
            return (date - modified) / 10
        if response.status in {300, 301, 308}:
            return self.MAXAGE
        return 0.0

    @staticmethod
    def _seconds(value: object) -> float | None:
        if value is None:
            return None
        if isinstance(value, bytes):
            value = value.decode("ascii", errors="ignore")
        try:
            return float(max(0, int(value)))
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _set_conditional_headers(
        cached_response: Response,
        request: Request,
    ) -> bool:
        etag = cached_response.headers.get("ETag")
        modified = cached_response.headers.get("Last-Modified")
        if etag is not None:
            request.headers["If-None-Match"] = etag
        if modified is not None:
            request.headers["If-Modified-Since"] = modified
        return False


class HttpCacheMiddleware:
    def __init__(
        self,
        settings: Settings,
        stats: object,
        policy: DummyPolicy,
        storage: NativeHttpCacheStorage,
    ) -> None:
        self.settings = settings
        self.stats = stats
        self.policy = policy
        self.storage = storage
        self.ignore_missing = settings.getbool("HTTPCACHE_IGNORE_MISSING")

    @classmethod
    def from_crawler(cls, crawler: Crawler) -> HttpCacheMiddleware:
        settings = crawler.settings
        if not settings.getbool("HTTPCACHE_ENABLED"):
            raise NotConfigured
        policy_type = load_object(str(settings["HTTPCACHE_POLICY"]))
        storage_type = load_object(str(settings["HTTPCACHE_STORAGE"]))
        storage = storage_type(settings)  # type: ignore[operator]
        middleware = cls(
            settings,
            crawler.stats,
            policy_type(settings),  # type: ignore[operator]
            storage,
        )
        crawler.signals.connect(middleware.spider_opened, spider_opened)
        crawler.signals.connect(middleware.spider_closed, spider_closed)
        return middleware

    def spider_opened(self, spider: Spider) -> None:
        self.storage.open_spider(spider)

    def spider_closed(self, spider: Spider, reason: str) -> None:
        self.storage.close_spider(spider)

    def process_request(self, request: Request, spider: Spider) -> Response | None:
        if request.meta.get("dont_cache", False):
            return None
        if not self.policy.should_cache_request(request):
            request.meta["_dont_cache"] = True
            return None
        try:
            cached_response = self.storage.retrieve_response(spider, request)
        except Exception:
            self.stats.inc_value("httpcache/retrieve_error")  # type: ignore[attr-defined]
            raise
        if cached_response is None:
            self.stats.inc_value("httpcache/miss")  # type: ignore[attr-defined]
            if self.ignore_missing:
                self.stats.inc_value("httpcache/ignore")  # type: ignore[attr-defined]
                raise IgnoreRequest(f"Ignored request not found in cache: {request}")
            return None
        if self.policy.is_cached_response_fresh(cached_response, request):
            self.stats.inc_value("httpcache/hit")  # type: ignore[attr-defined]
            return cached_response
        request.meta["cached_response"] = cached_response.replace(request=None)
        return None

    def process_response(
        self,
        request: Request,
        response: Response,
        spider: Spider,
    ) -> Response:
        if request.meta.get("dont_cache", False):
            return response
        if "_dont_cache" in request.meta or "cached" in response.flags:
            request.meta.pop("_dont_cache", None)
            return response
        if response.headers.get("Date") is None:
            response.headers["Date"] = formatdate(usegmt=True)
        cached = request.meta.pop("cached_response", None)
        if isinstance(cached, Response):
            if self.policy.is_cached_response_valid(cached, response, request):
                self.stats.inc_value("httpcache/revalidate")  # type: ignore[attr-defined]
                return cached.replace(request=request)
            self.stats.inc_value("httpcache/invalidate")  # type: ignore[attr-defined]
        else:
            self.stats.inc_value("httpcache/firsthand")  # type: ignore[attr-defined]
        if self.policy.should_cache_response(response, request):
            self.storage.store_response(spider, request, response)
            self.stats.inc_value("httpcache/store")  # type: ignore[attr-defined]
        else:
            self.stats.inc_value("httpcache/uncacheable")  # type: ignore[attr-defined]
        return response

    def process_exception(
        self,
        request: Request,
        exception: Exception,
        spider: Spider,
    ) -> Response | None:
        cached = request.meta.pop("cached_response", None)
        if isinstance(cached, Response) and isinstance(
            exception,
            (DownloadError, OSError, TimeoutError),
        ):
            self.stats.inc_value("httpcache/errorrecovery")  # type: ignore[attr-defined]
            return cached.replace(request=request)
        return None


__all__ = [
    "DummyPolicy",
    "HttpCacheMiddleware",
    "NativeHttpCacheStorage",
    "RFC2616Policy",
]
