from __future__ import annotations

import base64
from urllib.parse import unquote, urlsplit, urlunsplit
from urllib.request import getproxies, proxy_bypass

from .exceptions import NotConfigured
from .http import Request


class HttpProxyMiddleware:
    def __init__(self, auth_encoding: str | None = "latin-1") -> None:
        self.auth_encoding = auth_encoding
        self.proxies: dict[str, tuple[bytes | None, str]] = {}
        for scheme, url in getproxies().items():
            try:
                self.proxies[scheme] = self._get_proxy(url, scheme)
            except (TypeError, ValueError):
                continue

    @classmethod
    def from_crawler(cls, crawler: object) -> HttpProxyMiddleware:
        settings = crawler.settings  # type: ignore[attr-defined]
        if not settings.getbool("HTTPPROXY_ENABLED"):
            raise NotConfigured
        return cls(settings.get("HTTPPROXY_AUTH_ENCODING"))

    def process_request(self, request: Request, spider: object = None) -> None:
        credentials: bytes | None = None
        proxy_url: str | None = None
        scheme: str | None = None

        if "proxy" in request.meta:
            if request.meta["proxy"] is not None:
                credentials, proxy_url = self._get_proxy(request.meta["proxy"], "")
        elif self.proxies:
            request_scheme = urlsplit(request.url).scheme.lower()
            hostname = urlsplit(request.url).hostname
            if (
                request_scheme not in {"http", "https"}
                or (hostname is not None and not proxy_bypass(hostname))
            ) and request_scheme in self.proxies:
                scheme = request_scheme
                credentials, proxy_url = self.proxies[request_scheme]

        self._set_proxy_and_credentials(request, proxy_url, credentials, scheme)
        return None

    def _get_proxy(self, url: object, original_scheme: str) -> tuple[bytes | None, str]:
        if not isinstance(url, str):
            raise TypeError("request.meta['proxy'] must be a string or None")

        parsed = urlsplit(url)
        if parsed.hostname is None and "://" not in url:
            parsed = urlsplit(f"//{url}")
        scheme = (parsed.scheme or original_scheme or "http").lower()
        if scheme not in {"http", "https"}:
            raise ValueError("proxy URL must use HTTP or HTTPS")
        if parsed.hostname is None:
            raise ValueError(f"proxy URL must include a hostname: {url!r}")

        host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
        netloc = host if parsed.port is None else f"{host}:{parsed.port}"
        proxy_url = urlunsplit((scheme, netloc, "", "", ""))

        credentials = None
        if parsed.username:
            encoding = self.auth_encoding or "utf-8"
            user_pass = f"{unquote(parsed.username)}:{unquote(parsed.password or '')}".encode(
                encoding
            )
            credentials = base64.b64encode(user_pass)
        return credentials, proxy_url

    @staticmethod
    def _set_proxy_and_credentials(
        request: Request,
        proxy_url: str | None,
        credentials: bytes | None,
        scheme: str | None,
    ) -> None:
        if scheme:
            request.meta["_scheme_proxy"] = True
        if proxy_url:
            request.meta["proxy"] = proxy_url
        elif request.meta.get("proxy") is not None:
            request.meta["proxy"] = None

        if credentials:
            request.headers["Proxy-Authorization"] = b"Basic " + credentials
            request.meta["_auth_proxy"] = proxy_url
        elif "_auth_proxy" in request.meta:
            if proxy_url != request.meta["_auth_proxy"]:
                request.headers.pop("Proxy-Authorization", None)
                del request.meta["_auth_proxy"]
        elif "Proxy-Authorization" in request.headers:
            if proxy_url:
                request.meta["_auth_proxy"] = proxy_url
            else:
                del request.headers["Proxy-Authorization"]
