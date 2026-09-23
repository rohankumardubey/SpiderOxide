from __future__ import annotations

from urllib.parse import SplitResult, urlsplit

from w3lib.html import get_meta_refresh

from . import signals
from .exceptions import IgnoreRequest, NotConfigured
from .http import HtmlResponse, Request, Response, _urljoin
from .settings import Settings
from .spidermiddlewares import RefererMiddleware

REDIRECT_STATUSES = {301, 302, 303, 307, 308}
DEFAULT_PORTS = {"http": 80, "https": 443}


class RedirectMiddleware:
    enabled_setting = "REDIRECT_ENABLED"

    def __init__(self, settings: Settings) -> None:
        if not settings.getbool(self.enabled_setting, True):
            raise NotConfigured
        self.max_redirect_times = settings.getint("REDIRECT_MAX_TIMES", 20)
        self.priority_adjust = settings.getint("REDIRECT_PRIORITY_ADJUST", 2)
        self._referer_spider_middleware: RefererMiddleware | None = None
        if self.max_redirect_times < 0:
            raise ValueError("REDIRECT_MAX_TIMES cannot be negative")

    @classmethod
    def from_crawler(cls, crawler: object) -> RedirectMiddleware:
        middleware = cls(crawler.settings)  # type: ignore[attr-defined]
        middleware.stats = crawler.stats  # type: ignore[attr-defined]
        middleware.crawler = crawler
        if hasattr(crawler, "signals"):
            crawler.signals.connect(  # type: ignore[attr-defined]
                middleware._engine_started,
                signal=signals.engine_started,
            )
        return middleware

    def _engine_started(self) -> None:
        engine = self.crawler.engine
        if engine is None:
            return
        self._referer_spider_middleware = next(
            (
                middleware
                for middleware in engine.spider_middleware.middleware
                if isinstance(middleware, RefererMiddleware)
            ),
            None,
        )

    def _handle_referer(self, request: Request, response: Response) -> None:
        request.headers.pop("Referer", None)
        if self._referer_spider_middleware is not None:
            self._referer_spider_middleware.get_processed_request(request, response)

    def process_response(
        self,
        request: Request,
        response: Response,
        spider: object,
    ) -> Request | Response:
        if (
            request.meta.get("dont_redirect", False)
            or request.meta.get("handle_httpstatus_all", False)
            or response.status in request.meta.get("handle_httpstatus_list", ())
            or response.status in getattr(spider, "handle_httpstatus_list", ())
            or response.status not in REDIRECT_STATUSES
            or "Location" not in response.headers
        ):
            return response

        location = response.headers["Location"].decode("latin-1").strip()
        redirected_url = _urljoin(request.url, location)
        source = urlsplit(request.url)
        redirected = urlsplit(redirected_url)
        if redirected.scheme not in {"http", "https", source.scheme}:
            return response
        if not redirected.fragment and source.fragment:
            redirected_url = _urljoin(redirected_url, f"#{source.fragment}")
        return self._redirect_to(
            request,
            response,
            redirected_url,
            response.status,
        )

    def _redirect_to(
        self,
        request: Request,
        response: Response,
        redirected_url: str,
        reason: object,
        *,
        force_get: bool = False,
    ) -> Request:
        source = urlsplit(request.url)
        redirected = urlsplit(redirected_url)

        redirect_times = int(request.meta.get("redirect_times", 0)) + 1
        redirect_ttl = int(request.meta.get("redirect_ttl", self.max_redirect_times))
        if not redirect_ttl or redirect_times > self.max_redirect_times:
            self.stats.inc_value("redirect/max_reached")
            raise IgnoreRequest("max redirections reached")

        method = request.method
        body = request.body
        headers = request.headers.copy()
        if (
            force_get
            or (response.status in {301, 302} and method == "POST")
            or (response.status == 303 and method not in {"GET", "HEAD"})
        ):
            method = "GET"
            body = b""
            for name in (
                "Content-Type",
                "Content-Length",
                "Content-Encoding",
                "Content-Language",
                "Content-Location",
            ):
                headers.pop(name, None)

        same_host = source.hostname == redirected.hostname
        if not same_host or redirected.scheme not in {source.scheme, "https"}:
            headers.pop("Cookie", None)
        if (
            source.scheme != redirected.scheme
            or not same_host
            or self._port(source) != self._port(redirected)
        ):
            headers.pop("Authorization", None)

        meta = dict(request.meta)
        meta["redirect_times"] = redirect_times
        meta["redirect_ttl"] = redirect_ttl - 1
        meta["redirect_urls"] = [*request.meta.get("redirect_urls", []), request.url]
        meta["redirect_reasons"] = [
            *request.meta.get("redirect_reasons", []),
            reason,
        ]
        meta.pop("download_latency", None)
        meta.pop("download_slot", None)

        self.stats.inc_value("redirect/count")
        self.stats.inc_value(f"redirect/reason_count/{reason}")
        redirected_request = request.replace(
            url=redirected_url,
            method=method,
            headers=headers,
            body=body,
            cookies={},
            meta=meta,
            priority=request.priority + self.priority_adjust,
            dont_filter=request.dont_filter,
        )
        self._handle_referer(redirected_request, response)
        return redirected_request

    @staticmethod
    def _port(parsed: SplitResult) -> int | None:
        return parsed.port or DEFAULT_PORTS.get(parsed.scheme)


class MetaRefreshMiddleware(RedirectMiddleware):
    enabled_setting = "METAREFRESH_ENABLED"

    def __init__(self, settings: Settings) -> None:
        super().__init__(settings)
        self.max_delay = settings.getfloat("METAREFRESH_MAXDELAY", 100.0)
        self.ignore_tags = settings.getlist(
            "METAREFRESH_IGNORE_TAGS",
            ["noscript"],
        )

    def process_response(
        self,
        request: Request,
        response: Response,
        spider: object,
    ) -> Request | Response:
        if (
            request.meta.get("dont_redirect", False)
            or request.method == "HEAD"
            or not isinstance(response, HtmlResponse)
            or urlsplit(request.url).scheme not in {"http", "https"}
        ):
            return response
        interval, redirected_url = get_meta_refresh(
            response.body,
            response.url,
            response.encoding,
            self.ignore_tags,
        )
        if redirected_url is None or interval is None or interval >= self.max_delay:
            return response
        if urlsplit(redirected_url).scheme not in {"http", "https"}:
            return response
        return self._redirect_to(
            request,
            response,
            redirected_url,
            "meta refresh",
            force_get=True,
        )
