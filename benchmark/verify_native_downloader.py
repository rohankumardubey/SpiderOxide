from __future__ import annotations

import asyncio
import gzip
import json
import sys
from contextlib import suppress
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from spideroxide import (
    Crawler,
    DownloadError,
    Headers,
    NativeCrawlEngine,
    Request,
    RustDownloader,
    Settings,
    Spider,
    TextResponse,
)


async def _read_request(
    reader: asyncio.StreamReader,
) -> tuple[str, str, list[tuple[str, str]], bytes]:
    head = await reader.readuntil(b"\r\n\r\n")
    lines = head.decode("latin-1").split("\r\n")
    method, target, _ = lines[0].split(" ", 2)
    headers = [tuple(line.split(":", 1)) for line in lines[1:] if line]
    normalized = [(name.strip(), value.strip()) for name, value in headers]
    content_length = next(
        (int(value) for name, value in normalized if name.lower() == "content-length"),
        0,
    )
    return method, target, normalized, await reader.readexactly(content_length)


async def _serve_request(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        method, target, headers, body = await _read_request(reader)
        if target == "/redirect":
            response = (
                b"HTTP/1.1 302 Found\r\n"
                b"Location: /final\r\n"
                b"Content-Length: 0\r\n"
                b"Connection: close\r\n\r\n"
            )
        elif target == "/final":
            payload = b'{"redirected": true}'
            response = (
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: application/json; charset=utf-8\r\n"
                b"Set-Cookie: a=1\r\n"
                b"Set-Cookie: b=2\r\n"
                + f"Content-Length: {len(payload)}\r\n".encode()
                + b"Connection: close\r\n\r\n"
                + payload
            )
        elif target == "/echo":
            repeated = [value for name, value in headers if name.lower() == "x-repeat"]
            cookie = next(
                (value for name, value in headers if name.lower() == "cookie"),
                "",
            )
            user_agent = next(
                (value for name, value in headers if name.lower() == "user-agent"),
                "",
            )
            payload = json.dumps(
                {
                    "method": method,
                    "body": body.decode(),
                    "repeated": repeated,
                    "cookie": cookie,
                    "user_agent": user_agent,
                }
            ).encode()
            response = (
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: application/json\r\n"
                + f"Content-Length: {len(payload)}\r\n".encode()
                + b"Connection: close\r\n\r\n"
                + payload
            )
        elif target == "/stream":
            response = (
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: application/json\r\n"
                b"Transfer-Encoding: chunked\r\n"
                b"Connection: close\r\n\r\n"
                b'A\r\n{"stream":\r\n'
                b"5\r\ntrue}\r\n"
                b"0\r\n\r\n"
            )
        elif target == "/gzip":
            payload = gzip.compress(b'{"compressed": true}')
            response = (
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: application/json\r\n"
                b"Content-Encoding: gzip\r\n"
                + f"Content-Length: {len(payload)}\r\n".encode()
                + b"Connection: close\r\n\r\n"
                + payload
            )
        elif target == "/set-cookie":
            response = (
                b"HTTP/1.1 200 OK\r\n"
                b"Set-Cookie: persisted=yes; Path=/\r\n"
                b"Content-Length: 0\r\n"
                b"Connection: close\r\n\r\n"
            )
        elif target == "/large":
            response = (
                b"HTTP/1.1 200 OK\r\nContent-Length: 10\r\nConnection: close\r\n\r\n0123456789"
            )
        elif target == "/slow":
            await asyncio.sleep(0.2)
            response = b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok"
        elif target == "/paced":
            writer.write(
                b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\nConnection: close\r\n\r\n"
            )
            for chunk in (b"one", b"two", b"three"):
                await asyncio.sleep(0.04)
                writer.write(f"{len(chunk):X}\r\n".encode() + chunk + b"\r\n")
                await writer.drain()
            writer.write(b"0\r\n\r\n")
            await writer.drain()
            return
        else:
            response = b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
        writer.write(response)
        await writer.drain()
    except ConnectionError:
        pass
    finally:
        writer.close()
        with suppress(ConnectionError):
            await writer.wait_closed()


async def _verify() -> None:
    server = await asyncio.start_server(_serve_request, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    base_url = f"http://127.0.0.1:{port}"
    try:
        downloader = RustDownloader(
            Settings(
                {
                    "DOWNLOAD_TIMEOUT": 2,
                    "USER_AGENT": "SpiderOxide-native-test",
                }
            )
        )
        echo_request = Request(
            f"{base_url}/echo",
            method="POST",
            headers=Headers({"X-Repeat": ["one", "two"]}),
            cookies={"session": "abc"},
            body=b"payload",
        )
        echo = await downloader.fetch(echo_request)
        assert isinstance(echo, TextResponse)
        assert echo.request is echo_request
        assert echo.protocol == "HTTP/1.1"
        assert echo.json() == {
            "method": "POST",
            "body": "payload",
            "repeated": ["one", "two"],
            "cookie": "session=abc",
            "user_agent": "SpiderOxide-native-test",
        }

        redirected = await downloader.fetch(Request(f"{base_url}/redirect"))
        assert isinstance(redirected, TextResponse)
        assert redirected.url == f"{base_url}/final"
        assert redirected.json() == {"redirected": True}
        assert redirected.headers.getlist("set-cookie") == [b"a=1", b"b=2"]

        streamed = await downloader.fetch(Request(f"{base_url}/stream"))
        assert isinstance(streamed, TextResponse)
        assert streamed.json() == {"stream": True}

        compressed = await downloader.fetch(Request(f"{base_url}/gzip"))
        assert isinstance(compressed, TextResponse)
        assert compressed.json() == {"compressed": True}
        assert compressed.headers["content-encoding"] == b"gzip"
        assert int(compressed.headers["content-length"]) == len(
            gzip.compress(b'{"compressed": true}')
        )

        await downloader.fetch(Request(f"{base_url}/set-cookie"))
        cookies = await downloader.fetch(Request(f"{base_url}/echo"))
        assert "persisted=yes" in cookies.json()["cookie"]
        merged_cookies = await downloader.fetch(
            Request(f"{base_url}/echo", cookies={"request": "yes"})
        )
        assert "persisted=yes" in merged_cookies.json()["cookie"]
        assert "request=yes" in merged_cookies.json()["cookie"]
        await downloader.close()

        limited = RustDownloader(Settings({"DOWNLOAD_MAXSIZE": 5}))
        for path in ("/large", "/stream"):
            try:
                await limited.fetch(Request(f"{base_url}{path}"))
            except DownloadError as error:
                assert "DOWNLOAD_MAXSIZE (5 bytes)" in str(error)
            else:
                raise AssertionError("native downloader accepted an oversized response")
        await limited.close()

        timeout_downloader = RustDownloader(Settings({"DOWNLOAD_TIMEOUT": 0.05}))
        try:
            await timeout_downloader.fetch(Request(f"{base_url}/slow"))
        except DownloadError:
            pass
        else:
            raise AssertionError("native downloader did not enforce DOWNLOAD_TIMEOUT")
        await timeout_downloader.close()

        paced_downloader = RustDownloader(Settings({"DOWNLOAD_TIMEOUT": 0.1}))
        paced = await paced_downloader.fetch(Request(f"{base_url}/paced"))
        assert paced.body == b"onetwothree"
        await paced_downloader.close()

        try:
            RustDownloader(Settings({"DOWNLOAD_TIMEOUT": 1e308}))
        except ValueError:
            pass
        else:
            raise AssertionError("native downloader accepted an unrepresentable timeout")

        class NativeSpider(Spider):
            name = "native-downloader"
            start_urls = [f"{base_url}/gzip"]

            def parse(self, response: TextResponse) -> dict[str, bool]:
                return response.json()

        crawler = Crawler(
            NativeSpider,
            {
                "DOWNLOADER_BACKEND": "rust",
                "ENGINE_BACKEND": "rust",
            },
        )
        result = await crawler.crawl()
        assert result.items == ({"compressed": True},)
        assert isinstance(crawler.engine, NativeCrawlEngine)
        assert isinstance(crawler.engine.downloader, RustDownloader)
    finally:
        server.close()
        await server.wait_closed()


if __name__ == "__main__":
    asyncio.run(_verify())
    print(
        "Native downloader passed: requests, redirects, compression, streaming, cookies, "
        "timeouts, limits, and crawler integration"
    )
