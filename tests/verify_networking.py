from __future__ import annotations

import asyncio
import json
import socket
import ssl
import sys
from contextlib import suppress
from pathlib import Path

import h2.config
import h2.connection
import h2.events

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from spideroxide import (
    CannotResolveHostError,
    DownloadCancelledError,
    DownloadConnectionRefusedError,
    DownloadFailedError,
    DownloadTimeoutError,
    HttpxDownloader,
    Request,
    ResponseDataLossError,
    RustDownloader,
    Settings,
    TextResponse,
    UnsupportedURLSchemeError,
)
from spideroxide.networking import CachingResolver

FIXTURES = ROOT / "tests" / "fixtures"


async def _read_request(reader: asyncio.StreamReader) -> str:
    request_head = await reader.readuntil(b"\r\n\r\n")
    return request_head.split(b"\r\n", 1)[0].decode("ascii")


async def _send_response(
    writer: asyncio.StreamWriter,
    *,
    body: bytes = b"",
    content_length: int | None = None,
) -> None:
    writer.write(
        b"HTTP/1.1 200 OK\r\n"
        + f"Content-Length: {len(body) if content_length is None else content_length}\r\n".encode()
        + b"Content-Type: application/json\r\n"
        + b"Connection: close\r\n\r\n"
        + body
    )
    await writer.drain()


async def _serve_ok(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        request_line = await _read_request(reader)
        peer = writer.get_extra_info("peername")
        await _send_response(
            writer,
            body=json.dumps(
                {
                    "request": request_line,
                    "peer": None if peer is None else peer[0],
                }
            ).encode(),
        )
    finally:
        writer.close()
        with suppress(ConnectionError):
            await writer.wait_closed()


async def _serve_dataloss(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        await _read_request(reader)
        await _send_response(writer, body=b"abc", content_length=10)
    finally:
        writer.close()
        with suppress(ConnectionError):
            await writer.wait_closed()


async def _serve_slow(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        await _read_request(reader)
        await asyncio.sleep(0.2)
        await _send_response(writer, body=b"{}")
    except ConnectionError:
        pass
    finally:
        writer.close()
        with suppress(ConnectionError):
            await writer.wait_closed()


async def _serve_http2(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    connection = h2.connection.H2Connection(
        config=h2.config.H2Configuration(client_side=False),
    )
    connection.initiate_connection()
    writer.write(connection.data_to_send())
    await writer.drain()
    try:
        while data := await reader.read(65535):
            for event in connection.receive_data(data):
                if isinstance(event, h2.events.StreamEnded):
                    body = b'{"http2": true}'
                    connection.send_headers(
                        event.stream_id,
                        [
                            (":status", "200"),
                            ("content-type", "application/json"),
                            ("content-length", str(len(body))),
                        ],
                    )
                    connection.send_data(event.stream_id, body, end_stream=True)
            writer.write(connection.data_to_send())
            await writer.drain()
    except ConnectionError:
        pass
    finally:
        writer.close()
        with suppress(ConnectionError):
            await writer.wait_closed()


def _verify_defaults() -> None:
    settings = Settings()
    expected = {
        "DNSCACHE_ENABLED": True,
        "DNSCACHE_SIZE": 10000,
        "DNS_TIMEOUT": 60,
        "DNS_RESOLVER": "scrapy.resolver.CachingThreadedResolver",
        "TWISTED_DNS_RESOLVER": "scrapy.resolver.CachingThreadedResolver",
        "DOWNLOAD_BIND_ADDRESS": None,
        "DOWNLOAD_FAIL_ON_DATALOSS": True,
        "DOWNLOAD_TLS_MAX_VERSION": None,
        "DOWNLOAD_TLS_MIN_VERSION": None,
        "DOWNLOAD_VERIFY_CERTIFICATES": False,
        "DOWNLOADER_CLIENTCONTEXTFACTORY": "SENTINEL",
        "DOWNLOADER_CLIENT_TLS_CIPHERS": "DEFAULT",
        "DOWNLOADER_CLIENT_TLS_METHOD": "TLS",
        "DOWNLOADER_CLIENT_TLS_VERBOSE_LOGGING": False,
        "DOWNLOADER_CLIENT_CERTIFICATE": None,
        "DOWNLOADER_CLIENT_KEY": None,
        "DOWNLOADER_CLIENT_KEY_PASSWORD": None,
        "HTTP2_MAX_FRAME_SIZE": 16384,
        "HTTPX_HTTP2_ENABLED": False,
    }
    assert {name: settings[name] for name in expected} == expected


async def _verify_resolver_settings() -> None:
    ipv4 = CachingResolver(Settings())
    assert ipv4.family == socket.AF_INET
    await ipv4.resolve("localhost", 80)
    assert ipv4.cache_size == 1

    disabled = CachingResolver(Settings({"DNSCACHE_ENABLED": False}))
    await disabled.resolve("localhost", 80)
    assert disabled.cache_size == 0

    dual_stack = CachingResolver(
        Settings({"DNS_RESOLVER": "scrapy.resolver.CachingHostnameResolver"})
    )
    assert dual_stack.family == socket.AF_UNSPEC

    bounded = CachingResolver(Settings({"DNSCACHE_SIZE": 1}))
    await bounded.resolve("localhost", 80)
    await bounded.resolve("localhost.", 80)
    assert bounded.cache_size == 1


async def _expect_error(
    downloader: HttpxDownloader | RustDownloader,
    request: Request,
    error_type: type[Exception],
) -> None:
    try:
        await downloader.fetch(request)
    except error_type:
        return
    except Exception as error:
        raise AssertionError(
            f"expected {error_type.__name__}, got {type(error).__name__}: {error}"
        ) from error
    raise AssertionError(f"expected {error_type.__name__}")


async def _verify_backend(
    downloader_type: type[HttpxDownloader] | type[RustDownloader],
    *,
    http_port: int,
    tls_port: int,
    dataloss_port: int,
    slow_port: int,
    refused_port: int,
    http2_port: int,
    mtls_port: int,
) -> None:
    try:
        invalid_factory = downloader_type(
            Settings({"DOWNLOADER_CLIENTCONTEXTFACTORY": "example.InvalidFactory"})
        )
    except ValueError:
        pass
    else:
        await invalid_factory.close()
        raise AssertionError("invalid TLS context factory was accepted")

    downloader = downloader_type(
        Settings(
            {
                "DOWNLOAD_BIND_ADDRESS": "127.0.0.1",
                "DNSCACHE_SIZE": 2,
            }
        )
    )
    try:
        for _ in range(2):
            response = await downloader.fetch(Request(f"http://localhost:{http_port}/"))
            assert isinstance(response, TextResponse)
            assert response.json()["peer"] == "127.0.0.1"
            assert str(response.ip_address) == "127.0.0.1", (
                downloader_type.__name__,
                response.ip_address,
            )
            assert response.protocol == "HTTP/1.1"
        if isinstance(downloader, HttpxDownloader):
            assert downloader._resolver.cache_size == 1
        else:
            assert downloader._client is not None
            assert downloader._client.dns_cache_size == 1

        tls_response = await downloader.fetch(Request(f"https://localhost:{tls_port}/"))
        assert tls_response.status == 200
        assert tls_response.certificate
        assert str(tls_response.ip_address) == "127.0.0.1"

        await _expect_error(
            downloader,
            Request(f"http://networking.invalid:{http_port}/"),
            CannotResolveHostError,
        )
        await _expect_error(
            downloader,
            Request(f"http://127.0.0.1:{refused_port}/"),
            DownloadConnectionRefusedError,
        )
        await _expect_error(
            downloader,
            Request("ftp://127.0.0.1/resource"),
            UnsupportedURLSchemeError,
        )
        await _expect_error(
            downloader,
            Request(
                f"http://127.0.0.1:{slow_port}/",
                meta={"download_timeout": 0.02},
            ),
            DownloadTimeoutError,
        )
        await _expect_error(
            downloader,
            Request(f"http://127.0.0.1:{dataloss_port}/"),
            ResponseDataLossError,
        )
        await _expect_error(
            downloader,
            Request(
                f"http://127.0.0.1:{http_port}/",
                meta={"download_maxsize": 1},
            ),
            DownloadCancelledError,
        )
        partial = await downloader.fetch(
            Request(
                f"http://127.0.0.1:{dataloss_port}/",
                meta={"download_fail_on_dataloss": False},
            )
        )
        assert partial.body == b"abc"
        assert "dataloss" in partial.flags
    finally:
        await downloader.close()

    verifying = downloader_type(Settings({"DOWNLOAD_VERIFY_CERTIFICATES": True}))
    try:
        await _expect_error(
            verifying,
            Request(f"https://localhost:{tls_port}/"),
            DownloadFailedError,
        )
    finally:
        await verifying.close()

    browser_like = downloader_type(
        Settings(
            {
                "DOWNLOADER_CLIENTCONTEXTFACTORY": (
                    "scrapy.core.downloader.contextfactory.BrowserLikeContextFactory"
                )
            }
        )
    )
    try:
        await _expect_error(
            browser_like,
            Request(f"https://localhost:{tls_port}/"),
            DownloadFailedError,
        )
    finally:
        await browser_like.close()

    tls13 = downloader_type(Settings({"DOWNLOAD_TLS_MIN_VERSION": "TLSv1.3"}))
    try:
        await _expect_error(
            tls13,
            Request(f"https://localhost:{tls_port}/"),
            DownloadFailedError,
        )
    finally:
        await tls13.close()

    http2 = downloader_type(Settings({"HTTPX_HTTP2_ENABLED": True}))
    try:
        response = await http2.fetch(Request(f"https://localhost:{http2_port}/"))
        assert isinstance(response, TextResponse)
        assert response.json() == {"http2": True}
        assert response.protocol == "HTTP/2"
    finally:
        await http2.close()

    missing_client_certificate = downloader_type(Settings())
    try:
        await _expect_error(
            missing_client_certificate,
            Request(f"https://localhost:{mtls_port}/"),
            DownloadFailedError,
        )
    finally:
        await missing_client_certificate.close()
    mutual_tls = downloader_type(
        Settings(
            {
                "DOWNLOADER_CLIENT_CERTIFICATE": FIXTURES / "networking-cert.pem",
                "DOWNLOADER_CLIENT_KEY": FIXTURES / "networking-key.pem",
            }
        )
    )
    try:
        response = await mutual_tls.fetch(Request(f"https://localhost:{mtls_port}/"))
        assert response.status == 200
    finally:
        await mutual_tls.close()


async def _verify() -> None:
    _verify_defaults()
    await _verify_resolver_settings()
    http_server = await asyncio.start_server(_serve_ok, "127.0.0.1", 0)
    dataloss_server = await asyncio.start_server(_serve_dataloss, "127.0.0.1", 0)
    slow_server = await asyncio.start_server(_serve_slow, "127.0.0.1", 0)

    tls_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls_context.maximum_version = ssl.TLSVersion.TLSv1_2
    tls_context.load_cert_chain(
        FIXTURES / "networking-cert.pem",
        FIXTURES / "networking-key.pem",
    )
    tls_server = await asyncio.start_server(
        _serve_ok,
        "127.0.0.1",
        0,
        ssl=tls_context,
    )
    http2_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    http2_context.load_cert_chain(
        FIXTURES / "networking-cert.pem",
        FIXTURES / "networking-key.pem",
    )
    http2_context.set_alpn_protocols(["h2"])
    http2_server = await asyncio.start_server(
        _serve_http2,
        "127.0.0.1",
        0,
        ssl=http2_context,
    )
    mtls_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    mtls_context.load_cert_chain(
        FIXTURES / "networking-cert.pem",
        FIXTURES / "networking-key.pem",
    )
    mtls_context.load_verify_locations(FIXTURES / "networking-cert.pem")
    mtls_context.verify_mode = ssl.CERT_REQUIRED
    mtls_server = await asyncio.start_server(
        _serve_ok,
        "127.0.0.1",
        0,
        ssl=mtls_context,
    )

    refused = await asyncio.start_server(_serve_ok, "127.0.0.1", 0)
    refused_port = refused.sockets[0].getsockname()[1]
    refused.close()
    await refused.wait_closed()

    try:
        for downloader_type in (HttpxDownloader, RustDownloader):
            await _verify_backend(
                downloader_type,
                http_port=http_server.sockets[0].getsockname()[1],
                tls_port=tls_server.sockets[0].getsockname()[1],
                dataloss_port=dataloss_server.sockets[0].getsockname()[1],
                slow_port=slow_server.sockets[0].getsockname()[1],
                refused_port=refused_port,
                http2_port=http2_server.sockets[0].getsockname()[1],
                mtls_port=mtls_server.sockets[0].getsockname()[1],
            )
    finally:
        for server in (
            http_server,
            tls_server,
            http2_server,
            mtls_server,
            dataloss_server,
            slow_server,
        ):
            server.close()
            await server.wait_closed()


if __name__ == "__main__":
    asyncio.run(_verify())
    print(
        "Networking configuration passed: Scrapy defaults, DNS caching, address families, bind "
        "addresses, TLS verification and versions, HTTP/1.1 and HTTP/2, diagnostics, typed "
        "failures, client certificates, timeouts, and response data-loss behavior across Python "
        "and Rust downloaders"
    )
