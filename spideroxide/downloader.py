from __future__ import annotations

import asyncio
import codecs
import math
import mimetypes
from collections.abc import Iterable
from typing import Protocol
from urllib.parse import urlsplit

import httpx

from .backend import BackendUnavailableError
from .cookies import _request_cookie_header
from .exceptions import DownloadError
from .headers import Headers
from .http import HtmlResponse, Request, Response, TextResponse, XmlResponse
from .settings import Settings


class Downloader(Protocol):
    async def fetch(self, request: Request) -> Response: ...


class _StatelessCookies(httpx.Cookies):
    def extract_cookies(self, response: httpx.Response) -> None:
        return None


def _response_type(
    headers: Headers,
    *,
    url: str,
    body: bytes,
) -> type[Response]:
    content_type = headers.get("Content-Type", b"").decode("latin-1").lower()
    media_type = content_type.partition(";")[0].strip()
    if media_type in {"text/html", "application/xhtml+xml"}:
        return HtmlResponse
    if "xml" in media_type:
        return XmlResponse
    if media_type.startswith("text/") or "json" in media_type or "javascript" in media_type:
        return TextResponse
    if media_type:
        return Response

    guessed_type, compression = mimetypes.guess_type(urlsplit(url).path)
    if compression is None:
        if guessed_type in {"text/html", "application/xhtml+xml"}:
            return HtmlResponse
        if guessed_type in {"application/xml", "text/xml"} or (
            guessed_type is not None and guessed_type.endswith("+xml")
        ):
            return XmlResponse
        if guessed_type is not None and (
            guessed_type.startswith("text/")
            or guessed_type == "application/json"
            or guessed_type.endswith("+json")
        ):
            return TextResponse

    sample = body[:4096]
    prefix = sample.lstrip()[:64].lower()
    for bom, encoding in (
        (codecs.BOM_UTF32_BE, "utf-32"),
        (codecs.BOM_UTF32_LE, "utf-32"),
        (codecs.BOM_UTF16_BE, "utf-16"),
        (codecs.BOM_UTF16_LE, "utf-16"),
        (codecs.BOM_UTF8, "utf-8-sig"),
    ):
        if sample.startswith(bom):
            decoded = sample.decode(encoding, errors="replace").lstrip().lower()
            if decoded.startswith("<?xml"):
                return XmlResponse
            if decoded.startswith(("<!doctype html", "<html")):
                return HtmlResponse
            return TextResponse
    if prefix.startswith(b"<?xml"):
        return XmlResponse
    if prefix.startswith((b"<!doctype html", b"<html")):
        return HtmlResponse
    if b"\x00" in sample:
        return Response
    try:
        sample.decode("utf-8")
    except UnicodeDecodeError:
        return Response
    return TextResponse


def _download_settings(settings: Settings) -> tuple[float, int, str]:
    timeout = settings.getfloat("DOWNLOAD_TIMEOUT", 180.0)
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("DOWNLOAD_TIMEOUT must be a positive finite number")
    max_size = settings.getint("DOWNLOAD_MAXSIZE", 0)
    if max_size < 0:
        raise ValueError("DOWNLOAD_MAXSIZE cannot be negative")
    return timeout, max_size, str(settings.get("USER_AGENT", "SpiderOxide/0.1"))


def _response(
    request: Request,
    *,
    url: str,
    status: int,
    header_pairs: Iterable[tuple[str, str | bytes]],
    body: bytes,
    protocol: str,
) -> Response:
    headers = Headers()
    for name, value in header_pairs:
        headers.appendlist(name, value)
    response_type = _response_type(headers, url=url, body=body)
    return response_type(
        url=url,
        status=status,
        headers=headers,
        body=body,
        request=request,
        protocol=protocol,
    )


def _proxy_details(request: Request) -> tuple[str | None, bytes | None]:
    proxy = request.meta.get("proxy")
    if proxy is None:
        return None, None
    if not isinstance(proxy, str):
        raise DownloadError("request.meta['proxy'] must be a string or None")
    authorization = request.headers.get("Proxy-Authorization")
    return proxy, authorization


def _transport_headers(
    request: Request,
    *,
    cookies_enabled: bool,
) -> list[tuple[bytes, bytes]]:
    pairs = [
        (name.encode("ascii"), value)
        for name, value in request.headers.to_raw_pairs()
        if name.lower() != "proxy-authorization"
    ]
    if (
        cookies_enabled
        and not request.meta.get("dont_merge_cookies", False)
        and not request.meta.get("_cookies_processed", False)
        and "Cookie" not in request.headers
    ):
        cookie_header = _request_cookie_header(request)
        if cookie_header is not None:
            pairs.append((b"Cookie", cookie_header))
    return pairs


class HttpxDownloader:
    def __init__(
        self,
        settings: Settings | None = None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.settings = settings or Settings()
        timeout, self.max_size, user_agent = _download_settings(self.settings)
        self._timeout = timeout
        self._user_agent = user_agent
        self._transport = transport
        self._cookies_enabled = self.settings.getbool("COOKIES_ENABLED", True)
        self.client = self._create_client()
        self._proxy_clients: dict[tuple[str, bytes | None], httpx.AsyncClient] = {}

    def _create_client(
        self,
        proxy: str | None = None,
        authorization: bytes | None = None,
    ) -> httpx.AsyncClient:
        proxy_config = (
            None
            if proxy is None
            else httpx.Proxy(
                proxy,
                headers=(
                    None if authorization is None else [(b"Proxy-Authorization", authorization)]
                ),
            )
        )
        client = httpx.AsyncClient(
            timeout=self._timeout,
            follow_redirects=False,
            headers={"User-Agent": self._user_agent} if self._user_agent else None,
            transport=self._transport,
            proxy=proxy_config,
            trust_env=False,
        )
        # HTTPX always creates a client jar; persistence belongs to CookiesMiddleware.
        client._cookies = _StatelessCookies()
        return client

    async def fetch(self, request: Request) -> Response:
        proxy, authorization = _proxy_details(request)
        if proxy is None:
            client = self.client
        else:
            key = (proxy, authorization)
            client = self._proxy_clients.get(key)
            if client is None:
                try:
                    client = self._create_client(proxy, authorization)
                except (TypeError, ValueError) as error:
                    raise DownloadError(f"invalid proxy URL: {error}") from error
                self._proxy_clients[key] = client

        started = asyncio.get_running_loop().time()
        try:
            async with client.stream(
                request.method,
                request.url,
                headers=_transport_headers(
                    request,
                    cookies_enabled=self._cookies_enabled,
                ),
                content=request.body or None,
            ) as raw_response:
                request.meta["download_latency"] = asyncio.get_running_loop().time() - started
                declared_size = int(raw_response.headers.get("Content-Length", 0))
                if self.max_size and declared_size > self.max_size:
                    raise DownloadError(
                        f"response exceeded DOWNLOAD_MAXSIZE ({self.max_size} bytes)"
                    )
                body = bytearray()
                async for chunk in raw_response.aiter_bytes():
                    body.extend(chunk)
                    if self.max_size and len(body) > self.max_size:
                        raise DownloadError(
                            f"response exceeded DOWNLOAD_MAXSIZE ({self.max_size} bytes)"
                        )

                return _response(
                    request,
                    url=str(raw_response.url),
                    status=raw_response.status_code,
                    header_pairs=raw_response.headers.multi_items(),
                    body=bytes(body),
                    protocol=raw_response.http_version,
                )
        except httpx.HTTPError as error:
            raise DownloadError(f"unable to download {request.url}: {error}") from error

    async def close(self) -> None:
        clients = [self.client, *self._proxy_clients.values()]
        self._proxy_clients.clear()
        await asyncio.gather(*(client.aclose() for client in clients))


def _request_headers(request: Request, *, cookies_enabled: bool) -> list[tuple[str, bytes]]:
    pairs = request.headers.to_raw_pairs()
    if (
        not cookies_enabled
        or request.meta.get("dont_merge_cookies", False)
        or request.meta.get("_cookies_processed", False)
        or "Cookie" in request.headers
    ):
        return pairs

    cookie_value = _request_cookie_header(request)
    if cookie_value is not None:
        pairs.append(("Cookie", cookie_value))
    return pairs


class RustDownloader:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or Settings()
        timeout, max_size, user_agent = _download_settings(self.settings)
        try:
            from ._native import NativeDownloadError, NativeHttpClient
        except ImportError as error:
            raise BackendUnavailableError(
                "Rust downloader requested but the extension is unavailable; "
                "run `maturin develop --release` or select the Python downloader"
            ) from error

        self._download_error: type[Exception] = NativeDownloadError
        self._cookies_enabled = self.settings.getbool("COOKIES_ENABLED", True)
        self._client: object | None = NativeHttpClient(timeout, max_size, user_agent)

    async def fetch(self, request: Request) -> Response:
        client = self._client
        if client is None:
            raise RuntimeError("downloader is closed")
        proxy, authorization = _proxy_details(request)
        try:
            raw_response = await client.fetch(
                request.url,
                request.method,
                _request_headers(request, cookies_enabled=self._cookies_enabled),
                request.body,
                None if proxy is None else (proxy, authorization),
            )
        except self._download_error as error:
            raise DownloadError(str(error)) from error

        request.meta["download_latency"] = raw_response.latency
        return _response(
            request,
            url=raw_response.url,
            status=raw_response.status,
            header_pairs=raw_response.headers,
            body=raw_response.body,
            protocol=raw_response.protocol,
        )

    async def close(self) -> None:
        self._client = None


def create_downloader(settings: Settings) -> Downloader:
    selected = str(settings.get("DOWNLOADER_BACKEND", "python")).strip().lower()
    if selected == "python":
        return HttpxDownloader(settings)
    if selected == "rust":
        return RustDownloader(settings)
    if selected == "auto":
        try:
            return RustDownloader(settings)
        except BackendUnavailableError:
            return HttpxDownloader(settings)
    raise ValueError(
        f"invalid downloader backend {selected!r}; expected 'python', 'rust', or 'auto'"
    )


UrllibDownloader = HttpxDownloader
