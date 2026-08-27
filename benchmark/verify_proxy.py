from __future__ import annotations

import asyncio
import json
import os
import sys
from collections.abc import Iterator, Mapping
from contextlib import contextmanager, suppress
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from spideroxide import (
    Crawler,
    Headers,
    HttpProxyMiddleware,
    HttpxDownloader,
    Request,
    Response,
    RustDownloader,
    Spider,
    TextResponse,
)


async def _read_request(
    reader: asyncio.StreamReader,
) -> tuple[str, str, list[tuple[str, str]], bytes]:
    head = await reader.readuntil(b"\r\n\r\n")
    lines = head.decode("latin-1").split("\r\n")
    method, target, _ = lines[0].split(" ", 2)
    headers = [tuple(part.strip() for part in line.split(":", 1)) for line in lines[1:] if line]
    content_length = next(
        (int(value) for name, value in headers if name.lower() == "content-length"),
        0,
    )
    return method, target, headers, await reader.readexactly(content_length)


def _header(headers: list[tuple[str, str]], name: str) -> str | None:
    return next(
        (value for current, value in headers if current.lower() == name.lower()),
        None,
    )


async def _send_response(
    writer: asyncio.StreamWriter,
    *,
    status: str = "200 OK",
    headers: Mapping[str, str] | None = None,
    body: bytes = b"",
) -> None:
    response_headers = {
        "Content-Length": str(len(body)),
        "Connection": "close",
        **dict(headers or {}),
    }
    writer.write(
        f"HTTP/1.1 {status}\r\n".encode()
        + b"".join(f"{name}: {value}\r\n".encode() for name, value in response_headers.items())
        + b"\r\n"
        + body
    )
    await writer.drain()


class ProxyFixture:
    def __init__(self) -> None:
        self.history: list[tuple[str, str, list[tuple[str, str]], bytes]] = []

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            method, target, headers, body = await _read_request(reader)
            self.history.append((method, target, headers, body))
            if _header(headers, "Proxy-Authorization") is None:
                await _send_response(
                    writer,
                    status="407 Proxy Authentication Required",
                    headers={"Proxy-Authenticate": 'Basic realm="fixture"'},
                )
            elif target.endswith("/set-cookie"):
                await _send_response(
                    writer,
                    headers={"Set-Cookie": "shared=yes; Path=/"},
                )
            elif target.endswith("/redirect"):
                await _send_response(
                    writer,
                    status="302 Found",
                    headers={"Location": "http://target.test/final"},
                )
            else:
                payload = json.dumps(
                    {
                        "method": method,
                        "target": target,
                        "authorization": _header(headers, "Proxy-Authorization"),
                        "cookie": _header(headers, "Cookie"),
                        "body": body.decode(),
                    }
                ).encode()
                await _send_response(
                    writer,
                    headers={"Content-Type": "application/json"},
                    body=payload,
                )
        except ConnectionError:
            pass
        finally:
            writer.close()
            with suppress(ConnectionError):
                await writer.wait_closed()


async def _serve_origin(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        method, target, headers, _ = await _read_request(reader)
        payload = json.dumps(
            {
                "method": method,
                "target": target,
                "direct": True,
                "authorization": _header(headers, "Proxy-Authorization"),
            }
        ).encode()
        await _send_response(
            writer,
            headers={"Content-Type": "application/json"},
            body=payload,
        )
    except ConnectionError:
        pass
    finally:
        writer.close()
        with suppress(ConnectionError):
            await writer.wait_closed()


class ProxySpider(Spider):
    name = "proxy"

    def __init__(self, *, url: str, meta: Mapping[str, object] | None = None) -> None:
        super().__init__()
        self.url = url
        self.meta = dict(meta or {})

    async def start(self):
        yield Request(self.url, callback=self.parse_response, meta=self.meta)

    def parse_response(self, response: Response) -> dict[str, object]:
        return response.json()  # type: ignore[attr-defined,no-any-return]


@contextmanager
def _proxy_environment(**values: str) -> Iterator[None]:
    proxy_names = {
        name
        for name in os.environ
        if name.lower() in {"http_proxy", "https_proxy", "all_proxy", "no_proxy"}
    }
    original = {name: os.environ[name] for name in proxy_names}
    for name in proxy_names:
        del os.environ[name]
    os.environ.update(values)
    try:
        yield
    finally:
        for name in values:
            os.environ.pop(name, None)
        os.environ.update(original)


def _verify_middleware_api(proxy_url: str) -> None:
    middleware = HttpProxyMiddleware()
    middleware.proxies = {}
    request = Request(
        "http://target.test/resource",
        meta={"proxy": f"http://us%40er:p%3Ass@{proxy_url}"},
    )
    middleware.process_request(request)
    assert request.meta["proxy"] == f"http://{proxy_url}"
    assert request.meta["_auth_proxy"] == f"http://{proxy_url}"
    assert request.headers["Proxy-Authorization"] == b"Basic dXNAZXI6cDpzcw=="

    bare = Request(
        "http://target.test/resource",
        meta={"proxy": proxy_url},
    )
    middleware.process_request(bare)
    assert bare.meta["proxy"] == f"http://{proxy_url}"

    request.meta["proxy"] = "http://other-proxy.test:8080"
    middleware.process_request(request)
    assert "Proxy-Authorization" not in request.headers
    assert "_auth_proxy" not in request.meta

    request.headers["Proxy-Authorization"] = b"must-not-leak"
    request.meta["proxy"] = None
    middleware.process_request(request)
    assert "Proxy-Authorization" not in request.headers

    for invalid in (42, "", "socks5://proxy.test:1080", "http:///missing"):
        candidate = Request("http://target.test/", meta={"proxy": invalid})
        try:
            middleware.process_request(candidate)
        except (TypeError, ValueError):
            pass
        else:
            raise AssertionError(f"proxy middleware accepted invalid proxy {invalid!r}")


async def _crawl(
    downloader_type: type[HttpxDownloader] | type[RustDownloader],
    *,
    url: str,
    meta: Mapping[str, object] | None = None,
    settings: Mapping[str, object] | None = None,
) -> tuple[Crawler, object]:
    crawler = Crawler(
        ProxySpider,
        {
            "CONCURRENT_REQUESTS": 1,
            **dict(settings or {}),
        },
        downloader=downloader_type(),
    )
    result = await crawler.crawl(url=url, meta=meta)
    return crawler, result


async def _verify_downloader_parity(proxy_address: str, fixture: ProxyFixture) -> None:
    authenticated_proxy = f"http://user:password@{proxy_address}"
    expected_auth = "Basic dXNlcjpwYXNzd29yZA=="
    for downloader_type in (HttpxDownloader, RustDownloader):
        before = len(fixture.history)
        crawler, result = await _crawl(
            downloader_type,
            url="http://target.test/redirect",
            meta={"proxy": authenticated_proxy},
        )
        assert result.items == (
            {
                "method": "GET",
                "target": "http://target.test/final",
                "authorization": expected_auth,
                "cookie": None,
                "body": "",
            },
        )
        requests = fixture.history[before:]
        assert len(requests) == 2
        assert [target for _, target, _, _ in requests] == [
            "http://target.test/redirect",
            "http://target.test/final",
        ]
        assert all(
            _header(headers, "Proxy-Authorization") == expected_auth
            for _, _, headers, _ in requests
        )
        assert result.stats["redirect/count"] == 1

        assert crawler.engine is not None
        downloader = crawler.engine.downloader
        if isinstance(downloader, HttpxDownloader):
            assert downloader.client.is_closed
            assert not downloader._proxy_clients
        else:
            assert isinstance(downloader, RustDownloader)
            assert downloader._client is None


async def _verify_proxy_client_pools(
    proxy_address: str,
    origin_url: str,
) -> None:
    middleware = HttpProxyMiddleware()
    middleware.proxies = {}
    for downloader_type in (HttpxDownloader, RustDownloader):
        downloader = downloader_type()
        for username, path in (
            ("first", "value"),
            ("first", "set-cookie"),
            ("second", "cookie"),
        ):
            request = Request(
                f"http://target.test/{path}",
                meta={"proxy": f"http://{username}:secret@{proxy_address}"},
            )
            middleware.process_request(request)
            response = await downloader.fetch(request)
            assert response.status == 200
            if path == "cookie":
                assert isinstance(response, TextResponse)
                assert response.json()["cookie"] == "shared=yes"

        if isinstance(downloader, HttpxDownloader):
            assert len(downloader._proxy_clients) == 2
            clients = list(downloader._proxy_clients.values())
        else:
            assert downloader._client is not None
            assert downloader._client.proxy_client_count == 2

        direct = Request(
            origin_url,
            headers=Headers({"Proxy-Authorization": "must-not-leak"}),
            meta={"proxy": None},
        )
        middleware.process_request(direct)
        response = await downloader.fetch(direct)
        assert isinstance(response, TextResponse)
        assert response.json()["authorization"] is None
        await downloader.close()
        if isinstance(downloader, HttpxDownloader):
            assert all(client.is_closed for client in clients)


async def _verify_environment_and_bypasses(
    proxy_address: str,
    fixture: ProxyFixture,
    origin_url: str,
) -> None:
    for downloader_type in (HttpxDownloader, RustDownloader):
        with _proxy_environment(
            http_proxy=f"http://env:secret@{proxy_address}",
            no_proxy="",
        ):
            before = len(fixture.history)
            _, proxied = await _crawl(
                downloader_type,
                url="http://environment.test/value",
            )
            assert proxied.items[0]["target"] == "http://environment.test/value"
            assert proxied.items[0]["authorization"] == "Basic ZW52OnNlY3JldA=="
            assert len(fixture.history) == before + 1

            _, bypassed = await _crawl(
                downloader_type,
                url=origin_url,
                meta={"proxy": None},
            )
            assert bypassed.items[0]["direct"] is True
            assert len(fixture.history) == before + 1

            _, disabled = await _crawl(
                downloader_type,
                url=origin_url,
                settings={"HTTPPROXY_ENABLED": False},
            )
            assert disabled.items[0]["direct"] is True
            assert len(fixture.history) == before + 1

        with _proxy_environment(
            http_proxy=f"http://env:secret@{proxy_address}",
            no_proxy="127.0.0.1",
        ):
            _, excluded = await _crawl(downloader_type, url=origin_url)
            assert excluded.items[0]["direct"] is True


async def _verify() -> None:
    fixture = ProxyFixture()
    proxy_server = await asyncio.start_server(fixture.handle, "127.0.0.1", 0)
    proxy_port = proxy_server.sockets[0].getsockname()[1]
    proxy_address = f"127.0.0.1:{proxy_port}"
    origin_server = await asyncio.start_server(_serve_origin, "127.0.0.1", 0)
    origin_port = origin_server.sockets[0].getsockname()[1]
    origin_url = f"http://127.0.0.1:{origin_port}/direct"
    try:
        _verify_middleware_api(proxy_address)
        await _verify_downloader_parity(proxy_address, fixture)
        await _verify_proxy_client_pools(proxy_address, origin_url)
        await _verify_environment_and_bypasses(proxy_address, fixture, origin_url)
    finally:
        proxy_server.close()
        origin_server.close()
        await proxy_server.wait_closed()
        await origin_server.wait_closed()


if __name__ == "__main__":
    asyncio.run(_verify())
    print(
        "Proxy support passed: explicit and environment proxies, authentication, redirects, "
        "bypasses, disabled middleware, Python and Rust downloaders, and cleanup"
    )
