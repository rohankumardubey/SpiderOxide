from __future__ import annotations

from urllib.parse import SplitResult, urlsplit

from .exceptions import IgnoreRequest, NotConfigured
from .http import Request, Response, _urljoin
from .settings import Settings

REDIRECT_STATUSES = {301, 302, 303, 307, 308}
DEFAULT_PORTS = {"http": 80, "https": 443}


class RedirectMiddleware:
    def __init__(self, settings: Settings) -> None:
        if not settings.getbool("REDIRECT_ENABLED", True):
            raise NotConfigured
        self.max_redirect_times = settings.getint("REDIRECT_MAX_TIMES", 20)
        self.priority_adjust = settings.getint("REDIRECT_PRIORITY_ADJUST", 2)
        if self.max_redirect_times < 0:
            raise ValueError("REDIRECT_MAX_TIMES cannot be negative")

    @classmethod
    def from_crawler(cls, crawler: object) -> RedirectMiddleware:
        middleware = cls(crawler.settings)  # type: ignore[attr-defined]
        middleware.stats = crawler.stats  # type: ignore[attr-defined]
        return middleware

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
            redirected = urlsplit(redirected_url)

        redirect_times = int(request.meta.get("redirect_times", 0)) + 1
        redirect_ttl = int(request.meta.get("redirect_ttl", self.max_redirect_times))
        if not redirect_ttl or redirect_times > self.max_redirect_times:
            self.stats.inc_value("redirect/max_reached")
            raise IgnoreRequest("max redirections reached")

        method = request.method
        body = request.body
        headers = request.headers.copy()
        if (response.status in {301, 302} and method == "POST") or (
            response.status == 303 and method not in {"GET", "HEAD"}
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
        headers.pop("Referer", None)

        meta = dict(request.meta)
        meta["redirect_times"] = redirect_times
        meta["redirect_ttl"] = redirect_ttl - 1
        meta["redirect_urls"] = [*request.meta.get("redirect_urls", []), request.url]
        meta["redirect_reasons"] = [
            *request.meta.get("redirect_reasons", []),
            response.status,
        ]
        meta.pop("download_latency", None)
        meta.pop("download_slot", None)

        self.stats.inc_value("redirect/count")
        self.stats.inc_value(f"redirect/reason_count/{response.status}")
        return request.replace(
            url=redirected_url,
            method=method,
            headers=headers,
            body=body,
            cookies={},
            meta=meta,
            priority=request.priority + self.priority_adjust,
            dont_filter=request.dont_filter,
        )

    @staticmethod
    def _port(parsed: SplitResult) -> int | None:
        return parsed.port or DEFAULT_PORTS.get(parsed.scheme)
