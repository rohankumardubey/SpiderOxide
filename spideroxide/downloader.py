from __future__ import annotations

import asyncio
import math
from collections.abc import Iterable
from http.cookiejar import CookieJar
from typing import Protocol

import httpx

from .backend import BackendUnavailableError
from .exceptions import DownloadError
from .headers import Headers
from .http import Request, Response, TextResponse
from .settings import Settings


class Downloader(Protocol):
    async def fetch(self, request: Request) -> Response: ...


def _textual_response(headers: Headers) -> bool:
    content_type = headers.get("Content-Type", b"").decode("latin-1").lower()
    return content_type.startswith("text/") or any(
        marker in content_type for marker in ("json", "xml", "javascript")
    )


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
    response_type = TextResponse if _textual_response(headers) else Response
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


def _transport_headers(request: Request) -> list[tuple[str, str]]:
    return [
        (name, value)
        for name, value in request.headers.to_http_pairs()
        if name.lower() != "proxy-authorization"
    ]


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
        self._cookie_jar = CookieJar()
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
        return httpx.AsyncClient(
            timeout=self._timeout,
            follow_redirects=False,
            headers={"User-Agent": self._user_agent} if self._user_agent else None,
            cookies=self._cookie_jar,
            transport=self._transport,
            proxy=proxy_config,
            trust_env=False,
        )

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
                headers=_transport_headers(request),
                content=request.body or None,
                cookies=request.cookies,
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


def _request_headers(request: Request) -> list[tuple[str, bytes]]:
    pairs = request.headers.to_raw_pairs()
    if not request.cookies:
        return pairs

    try:
        cookie_value = "; ".join(
            f"{name}={value}" for name, value in request.cookies.items()
        ).encode("latin-1")
    except UnicodeEncodeError as error:
        raise DownloadError("cookie names and values must use Latin-1 characters") from error

    for index, (name, value) in enumerate(pairs):
        if name.lower() == "cookie":
            pairs[index] = (name, value + b"; " + cookie_value)
            break
    else:
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
                _request_headers(request),
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
