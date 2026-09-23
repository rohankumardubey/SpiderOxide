from __future__ import annotations

import asyncio
import gzip
import logging
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from spideroxide import (
    Crawler,
    DefaultHeadersMiddleware,
    DownloadError,
    DownloaderStatsMiddleware,
    DownloadTimeoutMiddleware,
    HtmlResponse,
    HttpAuthMiddleware,
    HttpCompressionMiddleware,
    HttpError,
    HttpErrorMiddleware,
    HttpxDownloader,
    IgnoreRequest,
    MetaCopyDetectionMiddleware,
    MetaRefreshMiddleware,
    OffsiteMiddleware,
    RedirectMiddleware,
    RefererMiddleware,
    Request,
    Response,
    RustDownloader,
    Settings,
    SignalManager,
    Spider,
    StartSpiderMiddleware,
    StatsCollector,
    UrlLengthMiddleware,
    UserAgentMiddleware,
)


class ExampleSpider:
    name = "example"
    allowed_domains = ["example.test"]


def _crawler(values: dict[str, object] | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        settings=Settings(values),
        stats=StatsCollector(),
        signals=SignalManager(),
        spider=ExampleSpider(),
        native_policy_runtime=None,
    )


def _verify_defaults() -> None:
    settings = Settings()
    downloader = settings.getdict("DOWNLOADER_MIDDLEWARES_BASE")
    assert downloader["spideroxide.downloadermiddlewares.OffsiteMiddleware"] == 50
    assert downloader["spideroxide.downloadermiddlewares.HttpAuthMiddleware"] == 300
    assert downloader["spideroxide.downloadermiddlewares.DownloadTimeoutMiddleware"] == 350
    assert downloader["spideroxide.downloadermiddlewares.DefaultHeadersMiddleware"] == 400
    assert downloader["spideroxide.downloadermiddlewares.UserAgentMiddleware"] == 500
    assert downloader["spideroxide.redirect.MetaRefreshMiddleware"] == 580
    assert downloader["spideroxide.downloadermiddlewares.HttpCompressionMiddleware"] == 590

    spider = settings.getdict("SPIDER_MIDDLEWARES_BASE")
    assert spider["spideroxide.spidermiddlewares.StartSpiderMiddleware"] == 25
    assert spider["spideroxide.spidermiddlewares.HttpErrorMiddleware"] == 50
    assert spider["spideroxide.spidermiddlewares.RefererMiddleware"] == 700
    assert spider["spideroxide.spidermiddlewares.UrlLengthMiddleware"] == 800
    assert spider["spideroxide.spidermiddlewares.MetaCopyDetectionMiddleware"] == 1000


def _verify_scrapy_differential() -> None:
    try:
        import scrapy.spidermiddlewares.referer as scrapy_referer
        from scrapy.settings.default_settings import (
            DOWNLOADER_MIDDLEWARES_BASE as SCRAPY_DOWNLOADER_MIDDLEWARES_BASE,
        )
        from scrapy.settings.default_settings import (
            SPIDER_MIDDLEWARES_BASE as SCRAPY_SPIDER_MIDDLEWARES_BASE,
        )
    except ImportError:
        return

    from spideroxide import spidermiddlewares as spideroxide_referer

    settings = Settings()
    downloader = settings.getdict("DOWNLOADER_MIDDLEWARES_BASE")
    spider = settings.getdict("SPIDER_MIDDLEWARES_BASE")
    for name in (
        "OffsiteMiddleware",
        "HttpAuthMiddleware",
        "DownloadTimeoutMiddleware",
        "DefaultHeadersMiddleware",
        "UserAgentMiddleware",
        "MetaRefreshMiddleware",
        "HttpCompressionMiddleware",
    ):
        expected = next(
            priority
            for path, priority in SCRAPY_DOWNLOADER_MIDDLEWARES_BASE.items()
            if path.endswith(f".{name}")
        )
        actual = next(
            priority for path, priority in downloader.items() if path.endswith(f".{name}")
        )
        assert actual == expected
    for name in (
        "StartSpiderMiddleware",
        "HttpErrorMiddleware",
        "RefererMiddleware",
        "UrlLengthMiddleware",
        "DepthMiddleware",
        "MetaCopyDetectionMiddleware",
    ):
        expected = next(
            priority
            for path, priority in SCRAPY_SPIDER_MIDDLEWARES_BASE.items()
            if path.endswith(f".{name}")
        )
        actual = next(priority for path, priority in spider.items() if path.endswith(f".{name}"))
        assert actual == expected

    pairs = (
        (
            "https://user:pass@example.test:443/path?q=1#fragment",
            "https://example.test/child",
        ),
        ("https://example.test/path", "http://example.test/child"),
        ("http://example.test:80/path", "https://other.test/child"),
        ("file:///tmp/source", "https://example.test/child"),
    )
    for name in (
        "NoReferrerPolicy",
        "NoReferrerWhenDowngradePolicy",
        "SameOriginPolicy",
        "OriginPolicy",
        "StrictOriginPolicy",
        "OriginWhenCrossOriginPolicy",
        "StrictOriginWhenCrossOriginPolicy",
        "UnsafeUrlPolicy",
        "DefaultReferrerPolicy",
    ):
        actual_policy = getattr(spideroxide_referer, name)()
        expected_policy = getattr(scrapy_referer, name)()
        for source, target in pairs:
            assert actual_policy.referrer(source, target) == expected_policy.referrer(
                source,
                target,
            )


def _verify_downloader_middleware() -> None:
    crawler = _crawler()
    spider = crawler.spider

    offsite = OffsiteMiddleware.from_crawler(crawler)
    offsite.spider_opened(spider)
    offsite.process_request(Request("https://sub.example.test/"), spider)
    offsite.process_request(
        Request("https://outside.test/", meta={"allow_offsite": True}),
        spider,
    )
    for request in (
        Request("https://outside.test/a"),
        Request("https://outside.test/b"),
    ):
        try:
            offsite.process_request(request, spider)
        except IgnoreRequest:
            pass
        else:
            raise AssertionError("offsite request was accepted")
    assert crawler.stats.get_value("offsite/domains") == 1
    assert crawler.stats.get_value("offsite/filtered") == 2

    auth_crawler = _crawler(
        {
            "HTTPAUTH_USER": "user",
            "HTTPAUTH_PASS": "pass",
            "HTTPAUTH_DOMAIN": "example.test",
        }
    )
    auth = HttpAuthMiddleware.from_crawler(auth_crawler)
    auth.spider_opened(spider)
    same_domain = Request("https://api.example.test/")
    auth.process_request(same_domain, spider)
    assert same_domain.headers["Authorization"] == b"Basic dXNlcjpwYXNz"
    outside = Request("https://outside.test/")
    auth.process_request(outside, spider)
    assert "Authorization" not in outside.headers
    override = Request(
        "https://outside.test/",
        meta={
            "http_user": "other",
            "http_pass": "secret",
            "http_auth_domain": None,
        },
    )
    auth.process_request(override, spider)
    assert override.headers["Authorization"] == b"Basic b3RoZXI6c2VjcmV0"
    existing = Request(
        "https://example.test/",
        headers={"Authorization": "Bearer token"},
    )
    auth.process_request(existing, spider)
    assert existing.headers["Authorization"] == b"Bearer token"

    timeout = DownloadTimeoutMiddleware.from_crawler(crawler)
    default_timeout = Request("https://example.test/")
    timeout.process_request(default_timeout, spider)
    assert default_timeout.meta["download_timeout"] == 180.0
    explicit_timeout = Request(
        "https://example.test/",
        meta={"download_timeout": 0.25},
    )
    timeout.process_request(explicit_timeout, spider)
    assert explicit_timeout.meta["download_timeout"] == 0.25

    default_headers = DefaultHeadersMiddleware.from_crawler(crawler)
    user_agent = UserAgentMiddleware.from_crawler(crawler)
    request = Request(
        "https://example.test/",
        headers={"Accept": "application/json", "User-Agent": "custom"},
    )
    default_headers.process_request(request, spider)
    user_agent.process_request(request, spider)
    assert request.headers["Accept"] == b"application/json"
    assert request.headers["Accept-Language"] == b"en"
    assert request.headers["User-Agent"] == b"custom"


def _verify_spider_middleware() -> None:
    crawler = _crawler()
    spider = crawler.spider
    request = Request("https://example.test/missing")
    response = Response(request.url, status=404, request=request)
    http_error = HttpErrorMiddleware.from_crawler(crawler)
    try:
        http_error.process_spider_input(response, spider)
    except HttpError as error:
        assert error.response is response
        assert list(http_error.process_spider_exception(response, error, spider) or ()) == []
    else:
        raise AssertionError("unhandled HTTP error response was accepted")
    assert crawler.stats.get_value("httperror/response_ignored_count") == 1
    allowed = Request(
        "https://example.test/allowed",
        meta={"handle_httpstatus_list": [404]},
    )
    http_error.process_spider_input(
        Response(allowed.url, status=404, request=allowed),
        spider,
    )

    referer = RefererMiddleware.from_crawler(crawler)
    parent_request = Request("https://user:pass@example.test:443/path?q=1#fragment")
    parent = Response(parent_request.url, request=parent_request)
    child = Request("https://example.test/child")
    referer.get_processed_request(child, parent)
    assert child.headers["Referer"] == b"https://example.test/path?q=1"
    downgrade = Request("http://example.test/insecure")
    referer.get_processed_request(downgrade, parent)
    assert "Referer" not in downgrade.headers
    origin_only = Request(
        "https://other.test/",
        meta={"referrer_policy": "origin"},
    )
    referer.get_processed_request(origin_only, parent)
    assert origin_only.headers["Referer"] == b"https://example.test/"

    length = UrlLengthMiddleware.from_crawler(crawler)
    assert length.get_processed_request(Request("https://example.test/"), parent)
    assert (
        length.get_processed_request(
            Request("https://example.test/" + "x" * 2100),
            parent,
        )
        is None
    )
    assert crawler.stats.get_value("urllength/request_ignored_count") == 1


async def _verify_start_and_engine_parity() -> None:
    start = StartSpiderMiddleware()

    async def requests():
        yield Request("https://example.test/")

    processed = [output async for output in start.process_start(requests(), ExampleSpider())]
    assert processed[0].meta["is_start_request"] is True

    length = UrlLengthMiddleware.from_crawler(_crawler({"URLLENGTH_LIMIT": 20}))
    filtered = [
        output
        async for output in length.process_start(
            requests(),
            ExampleSpider(),
        )
    ]
    assert filtered == []

    for backend in ("python", "rust"):
        downloader = RecordingDownloader()
        crawler = Crawler(
            BuiltinSpider,
            {"ENGINE_BACKEND": backend},
            downloader=downloader,
        )
        result = await crawler.crawl()
        assert result.items == ({"source": "child"},)
        assert downloader.requests[0].url == "https://example.test/root"
        assert {request.url for request in downloader.requests[1:]} == {
            "https://example.test/child",
            "https://example.test/missing",
        }
        by_url = {request.url: request for request in downloader.requests}
        root = by_url["https://example.test/root"]
        child = by_url["https://example.test/child"]
        missing = by_url["https://example.test/missing"]
        assert root.meta["is_start_request"] is True
        assert "is_start_request" not in child.meta
        assert child.headers["Referer"] == b"https://example.test/root"
        assert missing.headers["Referer"] == b"https://example.test/root"
        assert child.headers["Accept-Language"] == b"en"
        assert child.headers["User-Agent"] == b"SpiderOxide/0.1"
        assert b"gzip" in child.headers["Accept-Encoding"]
        assert crawler.stats.get_value("offsite/filtered") == 1
        assert crawler.stats.get_value("urllength/request_ignored_count") == 1
        assert crawler.stats.get_value("httperror/response_ignored_count") == 1

        disabled = Crawler(
            StartDisabledSpider,
            {"ENGINE_BACKEND": backend},
            downloader=RecordingDownloader(),
        )
        disabled_result = await disabled.crawl()
        assert disabled_result.items == ({"is_start_request": False},)


class RecordingDownloader:
    def __init__(self) -> None:
        self.requests: list[Request] = []

    async def fetch(self, request: Request) -> Response:
        self.requests.append(request.copy())
        status = 404 if request.url.endswith("/missing") else 200
        return HtmlResponse(
            request.url,
            status=status,
            body=b"<html></html>",
            encoding="utf-8",
            request=request,
        )

    async def close(self) -> None:
        return None


class BuiltinSpider(Spider):
    name = "builtin"
    allowed_domains = ["example.test"]
    custom_settings = {"URLLENGTH_LIMIT": 100}

    async def start(self):
        yield Request("https://example.test/root", callback=self.parse_root)

    def parse_root(self, response: Response):
        yield Request("https://example.test/child", callback=self.parse_child)
        yield Request("https://example.test/missing", callback=self.parse_missing)
        yield Request("https://outside.test/")
        yield Request("https://example.test/" + "x" * 101)

    def parse_child(self, response: Response) -> dict[str, str]:
        return {"source": "child"}

    def parse_missing(self, response: Response) -> dict[str, str]:
        return {"source": "missing"}


class StartDisabledSpider(Spider):
    name = "start-disabled"
    custom_settings = {
        "SPIDER_MIDDLEWARES": {
            StartSpiderMiddleware: None,
        }
    }

    async def start(self):
        yield Request("https://example.test/root")

    def parse(self, response: Response) -> dict[str, bool]:
        return {
            "is_start_request": bool(response.meta.get("is_start_request", False)),
        }


def _verify_compression_redirect_and_stats() -> None:
    crawler = _crawler()
    spider = crawler.spider
    compression = HttpCompressionMiddleware.from_crawler(crawler)
    request = Request("https://example.test/data")
    compression.process_request(request, spider)
    assert b"gzip" in request.headers["Accept-Encoding"]
    body = gzip.compress(b'{"ok": true}')
    response = Response(
        request.url,
        headers={"Content-Encoding": "gzip", "Content-Length": str(len(body))},
        body=body,
        request=request,
    )
    decoded = compression.process_response(request, response, spider)
    assert decoded.body == b'{"ok": true}'
    assert "Content-Encoding" not in decoded.headers
    assert decoded.headers["Content-Length"] == str(len(body)).encode()
    assert crawler.stats.get_value("httpcompression/response_count") == 1
    limited_request = Request(
        "https://example.test/large",
        meta={"download_maxsize": 10},
    )
    limited_body = gzip.compress(b"x" * 100)
    try:
        compression.process_response(
            limited_request,
            Response(
                limited_request.url,
                headers={"Content-Encoding": "gzip"},
                body=limited_body,
                request=limited_request,
            ),
            spider,
        )
    except IgnoreRequest:
        pass
    else:
        raise AssertionError("oversized decompressed response was accepted")

    stats = DownloaderStatsMiddleware.from_crawler(crawler)
    stats.process_request(request, spider)
    stats.process_response(request, decoded, spider)
    assert crawler.stats.get_value("downloader/request_bytes") > len(request.body)
    assert crawler.stats.get_value("downloader/response_bytes") > len(decoded.body)

    redirect = MetaRefreshMiddleware.from_crawler(crawler)
    html_request = Request(
        "https://example.test/source",
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        body=b"key=value",
    )
    html = HtmlResponse(
        html_request.url,
        body=b'<meta http-equiv="refresh" content="0; url=/target">',
        encoding="utf-8",
        request=html_request,
    )
    redirected = redirect.process_response(html_request, html, spider)
    assert isinstance(redirected, Request)
    assert redirected.url == "https://example.test/target"
    assert redirected.method == "GET"
    assert redirected.body == b""
    assert "Content-Type" not in redirected.headers
    assert redirected.meta["redirect_reasons"] == ["meta refresh"]

    http_redirect = RedirectMiddleware.from_crawler(crawler)
    http_redirect._referer_spider_middleware = RefererMiddleware.from_crawler(crawler)
    source_request = Request(
        "https://example.test/source",
        headers={"Referer": "https://old.test/"},
    )
    redirect_response = Response(
        source_request.url,
        status=302,
        headers={"Location": "https://other.test/target"},
        request=source_request,
    )
    redirected = http_redirect.process_response(source_request, redirect_response, spider)
    assert isinstance(redirected, Request)
    assert redirected.headers["Referer"] == b"https://example.test/source"


def _verify_meta_copy_warning() -> None:
    crawler = _crawler()
    middleware = MetaCopyDetectionMiddleware.from_crawler(crawler)
    records: list[logging.LogRecord] = []

    class Handler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    handler = Handler()
    logger = logging.getLogger("spideroxide.spidermiddlewares")
    logger.addHandler(handler)
    try:
        response_request = Request("https://example.test/source")
        response = Response(response_request.url, request=response_request)
        middleware.get_processed_request(
            Request(
                "https://example.test/target",
                meta={"retry_times": 1},
            ),
            response,
        )
        middleware.get_processed_request(
            Request(
                "https://example.test/target-2",
                meta={"redirect_times": 1},
            ),
            response,
        )
    finally:
        logger.removeHandler(handler)
    assert len(records) == 1
    assert "retry_times" in records[0].getMessage()


async def _verify_request_timeouts() -> None:
    async def handle(
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        try:
            await reader.readuntil(b"\r\n\r\n")
            await asyncio.sleep(0.2)
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok")
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        for downloader_type in (HttpxDownloader, RustDownloader):
            downloader = downloader_type(Settings({"DOWNLOAD_TIMEOUT": 1.0}))
            try:
                await downloader.fetch(
                    Request(
                        f"http://127.0.0.1:{port}/",
                        meta={"download_timeout": 0.05},
                    )
                )
            except DownloadError as error:
                assert "timed out" in str(error).lower() or "timeout" in str(error).lower()
            else:
                raise AssertionError(f"{downloader_type.__name__} ignored request timeout")
            finally:
                await downloader.close()
    finally:
        server.close()
        await server.wait_closed()


async def _verify() -> None:
    _verify_defaults()
    _verify_scrapy_differential()
    _verify_downloader_middleware()
    _verify_spider_middleware()
    _verify_compression_redirect_and_stats()
    _verify_meta_copy_warning()
    await _verify_start_and_engine_parity()
    await _verify_request_timeouts()


if __name__ == "__main__":
    asyncio.run(_verify())
    print(
        "Built-in middleware passed: defaults, filtering, auth, headers, compression, "
        "HTTP errors, referrers, redirects, stats, timeouts, and engine parity"
    )
