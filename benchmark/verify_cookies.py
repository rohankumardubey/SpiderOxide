from __future__ import annotations

import asyncio
import json
import sys
from contextlib import suppress
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from spideroxide._native import NativeCookieJar

from spideroxide import (
    CookiesMiddleware,
    Crawler,
    HttpxDownloader,
    Request,
    RustDownloader,
    Settings,
    Spider,
    TextResponse,
)
from spideroxide.job import deserialize_request, serialize_request


def _cookie_values(header: bytes | None) -> dict[str, str]:
    if header is None:
        return {}
    values = {}
    for entry in header.decode("latin-1").split(";"):
        name, separator, value = entry.strip().partition("=")
        if separator:
            values[name] = value
    return values


class RecordingDownloader:
    def __init__(self) -> None:
        self.requests: list[Request] = []
        self.closed = False

    async def fetch(self, request: Request) -> TextResponse:
        self.requests.append(request)
        path = urlsplit(request.url).path
        headers: list[tuple[str, str]] = [("Content-Type", "text/plain")]
        status = 200
        if path == "/set":
            headers.extend(
                [
                    ("Set-Cookie", "session=stored; Path=/"),
                    ("Set-Cookie", "scoped=allowed; Path=/allowed"),
                    ("Set-Cookie", "secure=secret; Secure; Path=/"),
                    ("Set-Cookie", "bad=ignored; Domain=com; Path=/"),
                ]
            )
        elif path == "/explicit":
            headers.append(("Set-Cookie", "session=updated; Path=/"))
        elif path == "/bypass":
            headers.append(("Set-Cookie", "bypass=ignored; Path=/"))
        elif path == "/redirect-set":
            status = 302
            headers.extend(
                [
                    ("Location", "/redirect-final"),
                    ("Set-Cookie", "redirect=stored; Path=/"),
                ]
            )
        elif path == "/redirect-overwrite":
            status = 302
            headers.extend(
                [
                    ("Location", "/redirect-overwrite-final"),
                    ("Set-Cookie", "overwrite=server; Path=/"),
                ]
            )
        return TextResponse(
            request.url,
            status=status,
            headers=headers,
            body=request.headers.get("Cookie", b""),
            encoding="latin-1",
            request=request,
        )

    async def close(self) -> None:
        self.closed = True


class CookieSpider(Spider):
    name = "cookies"

    def __init__(self, base_url: str) -> None:
        super().__init__()
        self.base_url = base_url

    async def start(self):
        yield Request(f"{self.base_url}/set", callback=self.after_set)

    def after_set(self, response: TextResponse) -> Request:
        return Request(
            f"{self.base_url}/default",
            callback=self.after_default,
            headers={"Cookie": "manual=discarded"},
        )

    def after_default(self, response: TextResponse) -> Request:
        assert _cookie_values(response.body) == {
            "session": "stored",
            "secure": "secret",
        }
        return Request(f"{self.base_url}/allowed/page", callback=self.after_allowed)

    def after_allowed(self, response: TextResponse) -> Request:
        assert _cookie_values(response.body) == {
            "session": "stored",
            "scoped": "allowed",
            "secure": "secret",
        }
        return Request(
            f"{self.base_url}/isolated",
            callback=self.after_isolated,
            cookies={"isolated": "yes"},
            meta={"cookiejar": "isolated"},
        )

    def after_isolated(self, response: TextResponse) -> Request:
        assert _cookie_values(response.body) == {"isolated": "yes"}
        return Request(
            f"{self.base_url}/isolated-again",
            callback=self.after_isolated_again,
            meta={"cookiejar": "isolated"},
        )

    def after_isolated_again(self, response: TextResponse) -> Request:
        assert _cookie_values(response.body) == {"isolated": "yes"}
        return Request(
            f"{self.base_url}/explicit",
            callback=self.after_explicit,
            cookies=[
                {"name": "explicit", "value": "yes"},
                {
                    "name": "path-cookie",
                    "value": "yes",
                    "path": "/explicit",
                },
            ],
        )

    def after_explicit(self, response: TextResponse) -> Request:
        assert _cookie_values(response.body) == {
            "session": "stored",
            "secure": "secret",
            "explicit": "yes",
            "path-cookie": "yes",
        }
        return Request(
            f"{self.base_url}/bypass",
            callback=self.after_bypass,
            headers={"Cookie": "raw=yes"},
            cookies={"request-cookie": "ignored"},
            meta={"dont_merge_cookies": True},
        )

    def after_bypass(self, response: TextResponse) -> Request:
        assert _cookie_values(response.body) == {"raw": "yes"}
        return Request(f"{self.base_url}/final", callback=self.parse_final)

    def parse_final(self, response: TextResponse) -> Request:
        cookies = _cookie_values(response.body)
        assert cookies == {
            "session": "updated",
            "secure": "secret",
            "explicit": "yes",
        }
        return Request(
            f"{self.base_url}/redirect-overwrite",
            callback=self.after_redirect_overwrite,
            cookies={"overwrite": "request"},
        )

    def after_redirect_overwrite(self, response: TextResponse) -> Request:
        cookies = _cookie_values(response.body)
        assert cookies["overwrite"] == "server"
        return Request(f"{self.base_url}/redirect-set", callback=self.parse_redirect)

    def parse_redirect(self, response: TextResponse) -> dict[str, object]:
        cookies = _cookie_values(response.body)
        assert cookies == {
            "session": "updated",
            "secure": "secret",
            "explicit": "yes",
            "overwrite": "server",
            "redirect": "stored",
        }
        return {"cookies": cookies}


class DisabledCookieSpider(Spider):
    name = "cookies-disabled"

    async def start(self):
        yield Request(
            "https://example.test/disabled",
            callback=self.parse,
            cookies={"ignored": "yes"},
        )

    def parse(self, response: TextResponse) -> dict[str, str]:
        assert response.body == b""
        return {"cookies": "disabled"}


def _verify_native_jar() -> None:
    jar = NativeCookieJar()
    assert jar.add_cookie("https://example.com/path", "host=yes; Path=/")
    assert jar.add_cookie("https://sub.example.com/path", "domain=yes; Domain=example.com")
    assert jar.add_cookie("https://sub.example.com/path", "scoped=yes; Path=/path")
    assert jar.add_cookie("https://sub.example.com/path", "secure=yes; Secure")
    assert not jar.add_cookie("https://sub.example.com/path", "public=no; Domain=com")
    assert jar.add_cookie("https://com/path", "host=yes; Domain=com")
    assert jar.add_cookie("https://example.com./path", "trailing=yes; Path=/")
    assert _cookie_values(jar.cookie_header("https://sub.example.com/path/next").encode()) == {
        "host": "yes",
        "domain": "yes",
        "trailing": "yes",
        "scoped": "yes",
        "secure": "yes",
    }
    assert _cookie_values(jar.cookie_header("http://example.com/other").encode()) == {
        "host": "yes",
        "domain": "yes",
        "trailing": "yes",
    }
    assert jar.add_cookie(
        "https://sub.example.com/path",
        "domain=; Domain=example.com; Max-Age=0",
    )
    assert len(jar) == 5
    jar.clear()
    assert len(jar) == 0


def _verify_explicit_secure_default() -> None:
    middleware = CookiesMiddleware()
    spider = CookieSpider("https://example.test")
    secure_request = Request(
        "https://example.test/set",
        cookies={"automatic-secure": "yes"},
    )
    middleware.process_request(secure_request, spider)
    assert _cookie_values(secure_request.headers.get("Cookie")) == {"automatic-secure": "yes"}

    insecure_request = Request("http://example.test/echo")
    middleware.process_request(insecure_request, spider)
    assert insecure_request.headers.get("Cookie") is None


def _verify_request_persistence() -> None:
    spider = CookieSpider("https://example.test")
    request = Request(
        "https://example.test/persist",
        cookies=[
            {
                "name": "session",
                "value": "stored",
                "domain": "example.test",
                "path": "/persist",
                "secure": True,
            }
        ],
    )
    restored = deserialize_request(serialize_request(request, spider), spider)
    assert restored.cookies == request.cookies
    assert restored.cookies is not request.cookies


async def _read_request(
    reader: asyncio.StreamReader,
) -> tuple[str, list[tuple[str, str]]]:
    head = await reader.readuntil(b"\r\n\r\n")
    lines = head.decode("latin-1").split("\r\n")
    target = lines[0].split(" ", 2)[1]
    headers = [tuple(line.split(":", 1)) for line in lines[1:] if line]
    return target, [(name.strip(), value.strip()) for name, value in headers]


async def _serve_request(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        target, headers = await _read_request(reader)
        cookie = next(
            (value for name, value in headers if name.lower() == "cookie"),
            "",
        )
        response_headers = ""
        if target == "/set":
            response_headers = "Set-Cookie: transport=stored; Path=/\r\n"
        payload = json.dumps({"cookie": cookie}).encode()
        writer.write(
            b"HTTP/1.1 200 OK\r\n"
            + response_headers.encode()
            + b"Content-Type: application/json\r\n"
            + f"Content-Length: {len(payload)}\r\n".encode()
            + b"Connection: close\r\n\r\n"
            + payload
        )
        await writer.drain()
    except ConnectionError:
        pass
    finally:
        writer.close()
        with suppress(ConnectionError):
            await writer.wait_closed()


async def _verify_transports() -> None:
    server = await asyncio.start_server(_serve_request, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    base_url = f"http://127.0.0.1:{port}"
    try:
        for downloader_type in (HttpxDownloader, RustDownloader):
            downloader = downloader_type(Settings({"DOWNLOAD_TIMEOUT": 2}))
            await downloader.fetch(Request(f"{base_url}/set"))
            unstored = await downloader.fetch(Request(f"{base_url}/echo"))
            assert isinstance(unstored, TextResponse)
            assert unstored.json()["cookie"] == ""

            middleware = CookiesMiddleware()
            spider = CookieSpider(base_url)
            set_request = Request(f"{base_url}/set")
            middleware.process_request(set_request, spider)
            set_response = await downloader.fetch(set_request)
            middleware.process_response(set_request, set_response, spider)

            echo_request = Request(f"{base_url}/echo")
            middleware.process_request(echo_request, spider)
            echoed = await downloader.fetch(echo_request)
            assert isinstance(echoed, TextResponse)
            assert _cookie_values(echoed.json()["cookie"].encode()) == {"transport": "stored"}

            bypass = Request(
                f"{base_url}/echo",
                cookies={"ignored": "yes"},
                meta={"dont_merge_cookies": True},
            )
            bypassed = await downloader.fetch(bypass)
            assert isinstance(bypassed, TextResponse)
            assert bypassed.json()["cookie"] == ""

            raw_and_cookies = Request(
                f"{base_url}/echo",
                headers={"Cookie": "raw=yes"},
                cookies={"ignored": "yes"},
            )
            raw = await downloader.fetch(raw_and_cookies)
            assert isinstance(raw, TextResponse)
            assert raw.json()["cookie"] == "raw=yes"

            verbose = await downloader.fetch(
                Request(
                    f"{base_url}/echo",
                    cookies=[{"name": "verbose", "value": "yes"}],
                )
            )
            assert isinstance(verbose, TextResponse)
            assert verbose.json()["cookie"] == "verbose=yes"
            await downloader.close()

            disabled = downloader_type(Settings({"COOKIES_ENABLED": False, "DOWNLOAD_TIMEOUT": 2}))
            ignored = await disabled.fetch(Request(f"{base_url}/echo", cookies={"ignored": "yes"}))
            assert isinstance(ignored, TextResponse)
            assert ignored.json()["cookie"] == ""
            await disabled.close()
    finally:
        server.close()
        await server.wait_closed()


async def _verify_engine(engine: str) -> None:
    downloader = RecordingDownloader()
    result = await Crawler(
        CookieSpider,
        {"ENGINE_BACKEND": engine},
        downloader=downloader,
    ).crawl(base_url="https://example.test")
    assert result.items == (
        {
            "cookies": {
                "session": "updated",
                "secure": "secret",
                "explicit": "yes",
                "overwrite": "server",
                "redirect": "stored",
            }
        },
    )
    assert downloader.closed

    disabled_downloader = RecordingDownloader()
    disabled = await Crawler(
        DisabledCookieSpider,
        {
            "ENGINE_BACKEND": engine,
            "COOKIES_ENABLED": False,
        },
        downloader=disabled_downloader,
    ).crawl()
    assert disabled.items == ({"cookies": "disabled"},)


async def _verify() -> None:
    _verify_native_jar()
    _verify_explicit_secure_default()
    _verify_request_persistence()
    await _verify_transports()
    for engine in ("python", "rust"):
        await _verify_engine(engine)


if __name__ == "__main__":
    asyncio.run(_verify())
    print(
        "Cookies passed: native storage, domains, paths, secure cookies, "
        "jar isolation, bypasses, transports, and engine parity"
    )
