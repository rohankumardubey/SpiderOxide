from __future__ import annotations

import asyncio
import codecs
import concurrent.futures
import logging
import math
import mimetypes
import socket
import threading
from collections.abc import Callable, Iterable
from ipaddress import ip_address
from typing import TYPE_CHECKING, Protocol
from urllib.parse import urlsplit

import httpx

from . import signals
from .backend import BackendUnavailableError
from .cookies import _request_cookie_header
from .exceptions import (
    CannotResolveHostError,
    DownloadCancelledError,
    DownloadConnectionRefusedError,
    DownloadError,
    DownloadFailedError,
    DownloadTimeoutError,
    ResponseDataLossError,
    StopDownload,
    UnsupportedURLSchemeError,
)
from .headers import Headers
from .http import HtmlResponse, Request, Response, TextResponse, XmlResponse
from .networking import (
    CachingResolver,
    bind_host,
    client_identity_pem,
    install_httpx_resolver,
    make_ssl_context,
    resolver_family,
    tls_debug_info,
    verifies_certificates,
)
from .settings import Settings

if TYPE_CHECKING:
    from .crawler import Crawler

logger = logging.getLogger(__name__)


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
    flags: Iterable[str] = (),
    certificate: bytes | None = None,
    remote_ip: str | None = None,
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
        flags=flags,
        certificate=certificate,
        ip_address=None if remote_ip is None else ip_address(remote_ip),
    )


def _stop_download(
    crawler: Crawler | None,
    signal: str,
    **kwargs: object,
) -> StopDownload | None:
    if crawler is None:
        return None
    responses = crawler.signals.send_sync(
        signal,
        dont_log=StopDownload,
        **kwargs,
    )
    return next(
        (
            response.exception
            for _, response in responses
            if isinstance(response, signals.SignalFailure)
            and isinstance(response.exception, StopDownload)
        ),
        None,
    )


def _call_on_loop_thread(
    loop: asyncio.AbstractEventLoop,
    loop_thread_id: int,
    callback: Callable[..., bool],
    *args: object,
) -> bool:
    if threading.get_ident() == loop_thread_id:
        return callback(*args)
    result: concurrent.futures.Future[bool] = concurrent.futures.Future()

    def invoke() -> None:
        try:
            result.set_result(callback(*args))
        except BaseException as error:
            result.set_exception(error)

    loop.call_soon_threadsafe(invoke)
    return result.result()


def _body_length(headers: Headers) -> int | None:
    value = headers.get("Content-Length")
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _proxy_details(
    request: Request,
) -> tuple[str | None, bytes | None, tuple[str, str] | None]:
    proxy = request.meta.get("proxy")
    if proxy is None:
        return None, None, None
    if not isinstance(proxy, str):
        raise DownloadError("request.meta['proxy'] must be a string or None")
    proxy_scheme = urlsplit(proxy).scheme.lower()
    if proxy_scheme not in {"http", "https", "socks4", "socks4a", "socks5", "socks5h"}:
        raise DownloadError("request.meta['proxy'] contains an unsupported proxy scheme")
    authorization = request.headers.get("Proxy-Authorization")
    proxy_auth = request.meta.get("_proxy_auth")
    if proxy_auth is not None and (
        not isinstance(proxy_auth, tuple)
        or len(proxy_auth) != 2
        or not all(isinstance(value, str) for value in proxy_auth)
    ):
        raise DownloadError("request.meta['_proxy_auth'] must contain proxy credentials")
    if proxy_scheme.startswith("socks"):
        authorization = None
    else:
        proxy_auth = None
    return proxy, authorization, proxy_auth


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


def _request_timeout(request: Request, default: float) -> float:
    value = request.meta.get("download_timeout", default)
    try:
        timeout = float(value)
    except (TypeError, ValueError) as error:
        raise DownloadError("download_timeout must be a positive finite number") from error
    if not math.isfinite(timeout) or timeout <= 0:
        raise DownloadError("download_timeout must be a positive finite number")
    return timeout


def _request_max_size(request: Request, default: int) -> int:
    value = request.meta.get("download_maxsize", default)
    try:
        max_size = int(value)
    except (TypeError, ValueError) as error:
        raise DownloadError("download_maxsize must be a non-negative integer") from error
    if max_size < 0:
        raise DownloadError("download_maxsize must be a non-negative integer")
    return max_size


def _request_warn_size(request: Request, default: int) -> int:
    value = request.meta.get("download_warnsize", default)
    try:
        warn_size = int(value)
    except (TypeError, ValueError) as error:
        raise DownloadError("download_warnsize must be a non-negative integer") from error
    if warn_size < 0:
        raise DownloadError("download_warnsize must be a non-negative integer")
    return warn_size


def _is_data_loss(error: Exception) -> bool:
    return isinstance(error, httpx.RemoteProtocolError) and (
        "incomplete message body" in str(error).lower()
        or "peer closed connection" in str(error).lower()
    )


def _httpx_download_error(request: Request, timeout: float, error: Exception) -> DownloadError:
    if isinstance(error, httpx.TimeoutException):
        return DownloadTimeoutError(f"Getting {request.url} timed out after {timeout} seconds.")
    if isinstance(error, httpx.UnsupportedProtocol):
        return UnsupportedURLSchemeError(str(error))
    if isinstance(error, httpx.ConnectError):
        cause: BaseException | None = error
        while cause is not None:
            if isinstance(cause, socket.gaierror):
                return CannotResolveHostError(str(error))
            if isinstance(cause, OSError) and cause.errno in {54, 61, 111}:
                return DownloadConnectionRefusedError(str(error))
            cause = cause.__cause__ or cause.__context__
        detail = str(error).lower()
        if "name or service not known" in detail or "nodename nor servname" in detail:
            return CannotResolveHostError(str(error))
        if "refused" in detail:
            return DownloadConnectionRefusedError(str(error))
    if isinstance(error, httpx.ProxyError):
        return DownloadConnectionRefusedError(str(error))
    return DownloadFailedError(str(error) or type(error).__name__)


class HttpxDownloader:
    def __init__(
        self,
        settings: Settings | None = None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        crawler: Crawler | None = None,
    ) -> None:
        self.settings = settings or Settings()
        timeout, self.max_size, user_agent = _download_settings(self.settings)
        self.warn_size = self.settings.getint("DOWNLOAD_WARNSIZE", 32 * 1024 * 1024)
        if self.warn_size < 0:
            raise ValueError("DOWNLOAD_WARNSIZE cannot be negative")
        self._timeout = timeout
        self._user_agent = user_agent
        self._transport = transport
        self._crawler = crawler
        self._standalone = crawler is None
        self._cookies_enabled = self.settings.getbool("COOKIES_ENABLED", True)
        self._fail_on_dataloss = self.settings.getbool("DOWNLOAD_FAIL_ON_DATALOSS", True)
        self._tls_verbose = self.settings.getbool(
            "DOWNLOADER_CLIENT_TLS_VERBOSE_LOGGING",
            False,
        )
        self._ssl_context = make_ssl_context(self.settings)
        self._resolver = CachingResolver(self.settings)
        self._http2 = self.settings.getbool("HTTPX_HTTP2_ENABLED", False)
        max_connections = self.settings.getint("CONCURRENT_REQUESTS", 16) or None
        self._limits = httpx.Limits(
            max_connections=max_connections,
            max_keepalive_connections=max_connections,
        )
        self._bind_host = bind_host(self.settings)
        self.client = self._create_client()
        self._proxy_clients: dict[
            tuple[str, bytes | None, tuple[str, str] | None],
            httpx.AsyncClient,
        ] = {}

    def _create_client(
        self,
        proxy: str | None = None,
        authorization: bytes | None = None,
        proxy_auth: tuple[str, str] | None = None,
    ) -> httpx.AsyncClient:
        transport = self._transport
        proxy_config = None
        if proxy is not None and urlsplit(proxy).scheme.startswith("socks"):
            if self._transport is not None:
                raise ValueError("custom HTTPX transports cannot be combined with SOCKS proxies")
            try:
                from httpx_socks import AsyncProxyTransport, ProxyType
            except ImportError as error:
                raise ValueError("SOCKS proxy support requires the httpx-socks package") from error
            username, password = proxy_auth or (None, None)
            parsed_proxy = urlsplit(proxy)
            transport = AsyncProxyTransport(
                proxy_type=(
                    ProxyType.SOCKS4
                    if parsed_proxy.scheme in {"socks4", "socks4a"}
                    else ProxyType.SOCKS5
                ),
                proxy_host=parsed_proxy.hostname or "",
                proxy_port=parsed_proxy.port or 1080,
                username=username,
                password=password,
                rdns=parsed_proxy.scheme in {"socks4a", "socks5h"},
                verify=self._ssl_context,
                limits=self._limits,
                trust_env=False,
            )
        elif self._transport is None:
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
            transport = httpx.AsyncHTTPTransport(
                verify=self._ssl_context,
                http2=self._http2,
                limits=self._limits,
                proxy=proxy_config,
                local_address=self._bind_host,
                trust_env=False,
            )
            install_httpx_resolver(transport, self._resolver)
        elif proxy is not None:
            raise ValueError("custom HTTPX transports cannot be combined with proxies")
        client = httpx.AsyncClient(
            timeout=self._timeout,
            follow_redirects=False,
            headers={"User-Agent": self._user_agent}
            if self._standalone and self._user_agent
            else None,
            transport=transport,
            trust_env=False,
        )
        if not self._standalone:
            client.headers.clear()
        # HTTPX always creates a client jar; persistence belongs to CookiesMiddleware.
        client._cookies = _StatelessCookies()
        return client

    async def fetch(self, request: Request) -> Response:
        timeout = _request_timeout(request, self._timeout)
        max_size = _request_max_size(request, self.max_size)
        warn_size = _request_warn_size(request, self.warn_size)
        fail_on_dataloss = bool(
            request.meta.get("download_fail_on_dataloss", self._fail_on_dataloss)
        )
        proxy, authorization, proxy_auth = _proxy_details(request)
        if proxy is None:
            client = self.client
        else:
            key = (proxy, authorization, proxy_auth)
            client = self._proxy_clients.get(key)
            if client is None:
                try:
                    client = self._create_client(proxy, authorization, proxy_auth)
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
                timeout=timeout,
            ) as raw_response:
                request.meta["download_latency"] = asyncio.get_running_loop().time() - started
                response_headers = Headers()
                for name, value in raw_response.headers.multi_items():
                    response_headers.appendlist(name, value)
                remote_ip, certificate, tls_detail = tls_debug_info(raw_response)
                if self._tls_verbose and tls_detail is not None:
                    logger.debug("SSL connection to %s using %s", request.url, tls_detail)
                stop = _stop_download(
                    self._crawler,
                    signals.headers_received,
                    headers=response_headers,
                    body_length=_body_length(response_headers),
                    request=request,
                    spider=None if self._crawler is None else self._crawler.spider,
                )
                declared_size = int(raw_response.headers.get("Content-Length", 0))
                if stop is None and max_size and declared_size > max_size:
                    raise DownloadCancelledError(
                        f"response exceeded DOWNLOAD_MAXSIZE ({max_size} bytes)"
                    )
                warned = bool(warn_size and declared_size > warn_size)
                if warned:
                    logger.warning(
                        "Expected response size (%s bytes) exceeds DOWNLOAD_WARNSIZE "
                        "(%s bytes) for %s",
                        declared_size,
                        warn_size,
                        request,
                    )
                body = bytearray()
                flags: tuple[str, ...] = ()
                if stop is None:
                    chunks = (
                        raw_response.aiter_bytes() if self._standalone else raw_response.aiter_raw()
                    )
                    try:
                        async for chunk in chunks:
                            body.extend(chunk)
                            stop = _stop_download(
                                self._crawler,
                                signals.bytes_received,
                                data=chunk,
                                request=request,
                                spider=None if self._crawler is None else self._crawler.spider,
                            )
                            if stop is not None:
                                break
                            if max_size and len(body) > max_size:
                                raise DownloadCancelledError(
                                    f"response exceeded DOWNLOAD_MAXSIZE ({max_size} bytes)"
                                )
                            if warn_size and len(body) > warn_size and not warned:
                                warned = True
                                logger.warning(
                                    "Received response size (%s bytes) exceeds "
                                    "DOWNLOAD_WARNSIZE (%s bytes) for %s",
                                    len(body),
                                    warn_size,
                                    request,
                                )
                    except httpx.HTTPError as error:
                        if not _is_data_loss(error):
                            raise
                        if fail_on_dataloss:
                            raise ResponseDataLossError(str(error)) from error
                        flags = ("dataloss",)

                response = _response(
                    request,
                    url=str(raw_response.url),
                    status=raw_response.status_code,
                    header_pairs=response_headers.to_raw_pairs(),
                    body=bytes(body),
                    protocol=raw_response.http_version,
                    flags=("download_stopped",) if stop is not None else flags,
                    certificate=certificate,
                    remote_ip=remote_ip,
                )
                if stop is not None:
                    stop.response = response
                    if stop.fail:
                        raise stop
                return response
        except DownloadError:
            raise
        except socket.gaierror as error:
            raise CannotResolveHostError(str(error)) from error
        except TimeoutError as error:
            raise DownloadTimeoutError(
                f"Getting {request.url} timed out after {timeout} seconds."
            ) from error
        except OSError as error:
            if error.errno in {54, 61, 111}:
                raise DownloadConnectionRefusedError(str(error)) from error
            raise DownloadFailedError(str(error)) from error
        except httpx.HTTPError as error:
            raise _httpx_download_error(request, timeout, error) from error

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
    def __init__(
        self,
        settings: Settings | None = None,
        *,
        crawler: Crawler | None = None,
    ) -> None:
        self.settings = settings or Settings()
        self._crawler = crawler
        timeout, max_size, user_agent = _download_settings(self.settings)
        warn_size = self.settings.getint("DOWNLOAD_WARNSIZE", 32 * 1024 * 1024)
        if warn_size < 0:
            raise ValueError("DOWNLOAD_WARNSIZE cannot be negative")
        ciphers = self.settings.get("DOWNLOADER_CLIENT_TLS_CIPHERS", "DEFAULT")
        if ciphers not in {None, "", "DEFAULT"}:
            raise ValueError("Rust downloader currently supports the default TLS cipher suite only")
        if self.settings.get("DOWNLOADER_CLIENT_TLS_METHOD", "TLS") != "TLS":
            raise ValueError("DOWNLOADER_CLIENT_TLS_METHOD must be 'TLS'")
        try:
            from ._native import (
                NativeCannotResolveHostError,
                NativeConnectionRefusedError,
                NativeDownloadCancelledError,
                NativeDownloadError,
                NativeDownloadTimeoutError,
                NativeHttpClient,
                NativeResponseDataLossError,
                NativeUnsupportedSchemeError,
            )
        except ImportError as error:
            raise BackendUnavailableError(
                "Rust downloader requested but the extension is unavailable; "
                "run `maturin develop --release` or select the Python downloader"
            ) from error

        self._download_error: type[Exception] = NativeDownloadError
        self._native_error_types = (
            (NativeDownloadCancelledError, DownloadCancelledError),
            (NativeDownloadTimeoutError, DownloadTimeoutError),
            (NativeCannotResolveHostError, CannotResolveHostError),
            (NativeConnectionRefusedError, DownloadConnectionRefusedError),
            (NativeUnsupportedSchemeError, UnsupportedURLSchemeError),
            (NativeResponseDataLossError, ResponseDataLossError),
        )
        self._cookies_enabled = self.settings.getbool("COOKIES_ENABLED", True)
        self._warn_size = warn_size
        self._fail_on_dataloss = self.settings.getbool("DOWNLOAD_FAIL_ON_DATALOSS", True)
        standalone = crawler is None
        minimum = self.settings.get("DOWNLOAD_TLS_MIN_VERSION")
        maximum = self.settings.get("DOWNLOAD_TLS_MAX_VERSION")
        self._client: object | None = NativeHttpClient(
            timeout,
            max_size,
            warn_size,
            user_agent if standalone else None,
            standalone,
            verifies_certificates(self.settings),
            client_identity_pem(self.settings),
            None if minimum is None else str(minimum),
            None if maximum is None else str(maximum),
            self.settings.getbool("HTTPX_HTTP2_ENABLED", False),
            bind_host(self.settings),
            self.settings.getint("CONCURRENT_REQUESTS_PER_DOMAIN", 8),
            self.settings.getbool("DOWNLOADER_CLIENT_TLS_VERBOSE_LOGGING", False),
            self.settings.getbool("DNSCACHE_ENABLED", True),
            self.settings.getint("DNSCACHE_SIZE", 10000),
            self.settings.getfloat("DNS_TIMEOUT", 60.0),
            resolver_family(self.settings) != socket.AF_INET,
            self._fail_on_dataloss,
        )

    async def fetch(self, request: Request) -> Response:
        client = self._client
        if client is None:
            raise RuntimeError("downloader is closed")
        timeout = _request_timeout(request, self.settings.getfloat("DOWNLOAD_TIMEOUT", 180.0))
        max_size = _request_max_size(
            request,
            self.settings.getint("DOWNLOAD_MAXSIZE", 0),
        )
        warn_size = _request_warn_size(request, self._warn_size)
        fail_on_dataloss = bool(
            request.meta.get("download_fail_on_dataloss", self._fail_on_dataloss)
        )
        proxy, authorization, proxy_auth = _proxy_details(request)
        stopped: list[StopDownload] = []
        loop = asyncio.get_running_loop()
        loop_thread_id = threading.get_ident()

        def headers_received(
            header_pairs: Iterable[tuple[str, bytes]],
            body_length: int | None,
        ) -> bool:
            headers = Headers(header_pairs)
            stop = _stop_download(
                self._crawler,
                signals.headers_received,
                headers=headers,
                body_length=body_length,
                request=request,
                spider=None if self._crawler is None else self._crawler.spider,
            )
            if stop is not None:
                stopped.append(stop)
            return stop is not None

        def bytes_received(data: bytes) -> bool:
            stop = _stop_download(
                self._crawler,
                signals.bytes_received,
                data=data,
                request=request,
                spider=None if self._crawler is None else self._crawler.spider,
            )
            if stop is not None:
                stopped.append(stop)
            return stop is not None

        def native_headers_received(
            header_pairs: Iterable[tuple[str, bytes]],
            body_length: int | None,
        ) -> bool:
            return _call_on_loop_thread(
                loop,
                loop_thread_id,
                headers_received,
                header_pairs,
                body_length,
            )

        def native_bytes_received(data: bytes) -> bool:
            return _call_on_loop_thread(
                loop,
                loop_thread_id,
                bytes_received,
                data,
            )

        native_headers_callback = native_headers_received if self._crawler is not None else None
        native_bytes_callback = native_bytes_received if self._crawler is not None else None

        try:
            raw_response = await client.fetch(
                request.url,
                request.method,
                _request_headers(request, cookies_enabled=self._cookies_enabled),
                request.body,
                None if proxy is None else (proxy, authorization, proxy_auth),
                native_headers_callback,
                native_bytes_callback,
                timeout,
                max_size,
                warn_size,
                fail_on_dataloss,
            )
        except self._download_error as error:
            for native_type, public_type in self._native_error_types:
                if isinstance(error, native_type):
                    raise public_type(str(error)) from error
            raise DownloadFailedError(str(error)) from error

        request.meta["download_latency"] = raw_response.latency
        if raw_response.warned:
            logger.warning(
                "Response size exceeds DOWNLOAD_WARNSIZE (%s bytes) for %s",
                warn_size,
                request,
            )
        flags = []
        if raw_response.stopped:
            flags.append("download_stopped")
        if raw_response.dataloss:
            flags.append("dataloss")
        response = _response(
            request,
            url=raw_response.url,
            status=raw_response.status,
            header_pairs=raw_response.headers,
            body=raw_response.body,
            protocol=raw_response.protocol,
            flags=flags,
            certificate=raw_response.certificate,
            remote_ip=raw_response.ip_address,
        )
        if stopped:
            stop = stopped[0]
            stop.response = response
            if stop.fail:
                raise stop
        return response

    async def close(self) -> None:
        self._client = None


def create_downloader(settings: Settings, *, crawler: Crawler | None = None) -> Downloader:
    selected = str(settings.get("DOWNLOADER_BACKEND", "python")).strip().lower()
    if selected == "python":
        return HttpxDownloader(settings, crawler=crawler)
    if selected == "rust":
        return RustDownloader(settings, crawler=crawler)
    if selected == "auto":
        try:
            return RustDownloader(settings, crawler=crawler)
        except BackendUnavailableError:
            return HttpxDownloader(settings, crawler=crawler)
    raise ValueError(
        f"invalid downloader backend {selected!r}; expected 'python', 'rust', or 'auto'"
    )


UrllibDownloader = HttpxDownloader
