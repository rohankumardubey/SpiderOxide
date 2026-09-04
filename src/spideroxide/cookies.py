from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING, Protocol
from urllib.parse import urlsplit

from .exceptions import NotConfigured
from .http import Request, Response

if TYPE_CHECKING:
    from .crawler import Crawler
    from .spider import Spider

logger = logging.getLogger(__name__)


class _CookieJar(Protocol):
    def add_cookie(self, url: str, value: str) -> bool: ...

    def cookie_header(self, url: str) -> str | None: ...


def _new_cookie_jar() -> _CookieJar:
    from ._native import NativeCookieJar

    return NativeCookieJar()


def _cookie_text(value: object, request: Request) -> str | None:
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8")
        except UnicodeDecodeError:
            logger.warning("Non UTF-8 encoded cookie found in request %s", request)
            return value.decode("latin-1", errors="replace")
    if isinstance(value, (bool, float, int, str)):
        return str(value)
    return None


def _verbose_cookies(request: Request) -> Iterable[Mapping[str, object]]:
    if isinstance(request.cookies, Mapping):
        return ({"name": name, "value": value} for name, value in request.cookies.items())
    return request.cookies


def _format_cookie(cookie: Mapping[str, object], request: Request) -> str | None:
    name = _cookie_text(cookie.get("name"), request)
    value = _cookie_text(cookie.get("value"), request)
    if name is None or value is None:
        missing = "name" if name is None else "value"
        logger.warning(
            "Invalid cookie found in request %s: %r (%s is missing)", request, cookie, missing
        )
        return None

    formatted = f"{name}={value}"
    for attribute in ("path", "domain"):
        raw_value = cookie.get(attribute)
        if raw_value is None:
            continue
        decoded = _cookie_text(raw_value, request)
        if decoded is None:
            logger.warning(
                "Invalid %s value in cookie for request %s: %r",
                attribute,
                request,
                raw_value,
            )
            return None
        formatted += f"; {attribute.capitalize()}={decoded}"
    if cookie.get("secure", urlsplit(request.url).scheme == "https"):
        formatted += "; Secure"
    return formatted


def _request_cookie_header(request: Request) -> bytes | None:
    jar = _new_cookie_jar()
    for cookie in _verbose_cookies(request):
        formatted = _format_cookie(cookie, request)
        if formatted is not None:
            jar.add_cookie(request.url, formatted)
    header = jar.cookie_header(request.url)
    return None if header is None else header.encode(request.encoding)


class CookiesMiddleware:
    def __init__(self, debug: bool = False) -> None:
        self.jars: defaultdict[object, _CookieJar] = defaultdict(_new_cookie_jar)
        self.debug = debug
        self.crawler: Crawler | None = None

    @classmethod
    def from_crawler(cls, crawler: Crawler) -> CookiesMiddleware:
        if not crawler.settings.getbool("COOKIES_ENABLED", True):
            raise NotConfigured
        middleware = cls(crawler.settings.getbool("COOKIES_DEBUG", False))
        middleware.crawler = crawler
        return middleware

    def process_request(self, request: Request, spider: Spider) -> None:
        if request.meta.get("dont_merge_cookies", False) or urlsplit(
            request.url
        ).scheme.lower() not in {
            "http",
            "https",
        }:
            return

        jar = self.jars[request.meta.get("cookiejar")]
        for cookie in _verbose_cookies(request):
            formatted = _format_cookie(cookie, request)
            if formatted is not None:
                jar.add_cookie(request.url, formatted)

        request.headers.pop("Cookie", None)
        header = jar.cookie_header(request.url)
        if header is not None:
            request.headers["Cookie"] = header
        request.meta["_cookies_processed"] = True
        self._debug_cookie(request, spider)

    def process_response(
        self,
        request: Request,
        response: Response,
        spider: Spider,
    ) -> Response:
        if request.meta.get("dont_merge_cookies", False) or urlsplit(
            request.url
        ).scheme.lower() not in {
            "http",
            "https",
        }:
            return response

        jar = self.jars[request.meta.get("cookiejar")]
        for value in response.headers.getlist("Set-Cookie"):
            jar.add_cookie(request.url, value.decode("latin-1"))
        self._debug_set_cookie(response, spider)
        return response

    def _debug_cookie(self, request: Request, spider: Spider) -> None:
        if not self.debug:
            return
        values = request.headers.getlist("Cookie")
        if values:
            rendered = "\n".join(
                f"Cookie: {value.decode('latin-1', errors='replace')}" for value in values
            )
            logger.debug("Sending cookies to: %s\n%s", request, rendered, extra={"spider": spider})

    def _debug_set_cookie(self, response: Response, spider: Spider) -> None:
        if not self.debug:
            return
        values = response.headers.getlist("Set-Cookie")
        if values:
            rendered = "\n".join(
                f"Set-Cookie: {value.decode('latin-1', errors='replace')}" for value in values
            )
            logger.debug(
                "Received cookies from: %s\n%s",
                response,
                rendered,
                extra={"spider": spider},
            )


__all__ = ["CookiesMiddleware"]
