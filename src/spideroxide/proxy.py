from __future__ import annotations

import base64
from urllib.parse import unquote, urlsplit, urlunsplit
from urllib.request import getproxies, proxy_bypass

from .exceptions import NotConfigured
from .http import Request

ProxyCredentials = bytes | tuple[str, str]
_PROXY_SCHEMES = {"http", "https", "socks4", "socks4a", "socks5", "socks5h"}
_SOCKS_SCHEMES = {"socks4", "socks4a", "socks5", "socks5h"}


class HttpProxyMiddleware:
    def __init__(self, auth_encoding: str | None = "latin-1") -> None:
        self.auth_encoding = auth_encoding
        self.proxies: dict[str, tuple[ProxyCredentials | None, str]] = {}
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
        credentials: ProxyCredentials | None = None
        proxy_url: str | None = None
        scheme: str | None = None

        if "proxy" in request.meta:
            if request.meta["proxy"] is not None:
                credentials, proxy_url = self._get_proxy(request.meta["proxy"], "")
        elif self.proxies:
            request_scheme = urlsplit(request.url).scheme.lower()
            hostname = urlsplit(request.url).hostname
            configured = self.proxies.get(request_scheme, self.proxies.get("all"))
            if (
                request_scheme in {"http", "https"}
                and configured is not None
                and (hostname is None or not proxy_bypass(hostname))
            ):
                scheme = request_scheme
                credentials, proxy_url = configured

        self._set_proxy_and_credentials(request, proxy_url, credentials, scheme)
        return None

    def _get_proxy(
        self,
        url: object,
        original_scheme: str,
    ) -> tuple[ProxyCredentials | None, str]:
        if not isinstance(url, str):
            raise TypeError("request.meta['proxy'] must be a string or None")

        parsed = urlsplit(url)
        if parsed.hostname is None and "://" not in url:
            parsed = urlsplit(f"//{url}")
        scheme = (
            parsed.scheme or ("http" if original_scheme == "all" else original_scheme) or "http"
        ).lower()
        if scheme not in _PROXY_SCHEMES:
            raise ValueError("proxy URL must use HTTP, HTTPS, SOCKS4, SOCKS4a, SOCKS5, or SOCKS5h")
        if parsed.hostname is None:
            raise ValueError(f"proxy URL must include a hostname: {url!r}")
        if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
            raise ValueError("proxy URL cannot contain a path, query, or fragment")

        host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
        netloc = host if parsed.port is None else f"{host}:{parsed.port}"
        proxy_url = urlunsplit((scheme, netloc, "", "", ""))

        credentials: ProxyCredentials | None = None
        if parsed.username is not None:
            username = unquote(parsed.username)
            password = unquote(parsed.password or "")
            if scheme in {"socks4", "socks4a"}:
                raise ValueError("SOCKS4 proxies do not support authentication")
            if scheme in _SOCKS_SCHEMES:
                credentials = (username, password)
            else:
                encoding = self.auth_encoding or "utf-8"
                credentials = base64.b64encode(f"{username}:{password}".encode(encoding))
        elif parsed.password is not None:
            raise ValueError("proxy password requires a username")
        return credentials, proxy_url

    def _set_proxy_and_credentials(
        self,
        request: Request,
        proxy_url: str | None,
        credentials: ProxyCredentials | None,
        scheme: str | None,
    ) -> None:
        proxy_scheme = urlsplit(proxy_url).scheme if proxy_url is not None else None
        if scheme:
            request.meta["_scheme_proxy"] = True
        if proxy_url:
            request.meta["proxy"] = proxy_url
        elif request.meta.get("proxy") is not None:
            request.meta["proxy"] = None

        previous_auth_proxy = request.meta.get("_auth_proxy")
        if proxy_scheme in _SOCKS_SCHEMES:
            request.headers.pop("Proxy-Authorization", None)
            if isinstance(credentials, tuple):
                request.meta["_proxy_auth"] = credentials
                request.meta["_auth_proxy"] = proxy_url
            else:
                request.meta.pop("_proxy_auth", None)
                request.meta.pop("_auth_proxy", None)
            return

        request.meta.pop("_proxy_auth", None)
        if isinstance(credentials, bytes):
            request.headers["Proxy-Authorization"] = b"Basic " + credentials
            request.meta["_auth_proxy"] = proxy_url
        elif previous_auth_proxy is not None:
            if proxy_url != previous_auth_proxy:
                request.headers.pop("Proxy-Authorization", None)
                request.meta.pop("_auth_proxy", None)
        elif "Proxy-Authorization" in request.headers:
            if proxy_url:
                request.meta["_auth_proxy"] = proxy_url
            else:
                del request.headers["Proxy-Authorization"]
