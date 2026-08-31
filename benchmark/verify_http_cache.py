from __future__ import annotations

import asyncio
import sys
import tempfile
import time
from email.utils import formatdate
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from spideroxide._native import NativeHttpCacheStore

from spideroxide import Crawler, HtmlResponse, Request, Response, Spider
from spideroxide.httpcache import (
    HttpCacheMiddleware,
    NativeHttpCacheStorage,
    RFC2616Policy,
)
from spideroxide.settings import Settings
from spideroxide.stats import StatsCollector


class RecordingDownloader:
    def __init__(
        self,
        *,
        status: int = 200,
        body: bytes = b"<html>live</html>",
        headers: object = None,
    ) -> None:
        self.status = status
        self.body = body
        self.headers = headers or {
            "Content-Type": "text/html; charset=utf-8",
            "X-Repeated": ["one", "two"],
        }
        self.requests: list[Request] = []
        self.closed = False

    async def fetch(self, request: Request) -> HtmlResponse:
        self.requests.append(request)
        return HtmlResponse(
            "https://example.test/final",
            status=self.status,
            headers=self.headers,
            body=self.body,
            encoding="utf-8",
            request=request,
        )

    async def close(self) -> None:
        self.closed = True


class CacheSpider(Spider):
    name = "http-cache"

    def __init__(self, *, meta: dict[str, object] | None = None) -> None:
        super().__init__()
        self.request_meta = meta or {}

    async def start(self):
        yield Request(
            "https://example.test/original",
            callback=self.parse,
            meta=self.request_meta,
            dont_filter=True,
        )

    def parse(self, response: HtmlResponse) -> dict[str, object]:
        return {
            "url": response.url,
            "body": response.text,
            "flags": response.flags,
            "headers": response.headers.getlist("X-Repeated"),
        }


def _settings(cache_dir: Path, engine: str, **values: object) -> dict[str, object]:
    return {
        "ENGINE_BACKEND": engine,
        "HTTPCACHE_ENABLED": True,
        "HTTPCACHE_DIR": str(cache_dir),
        "RETRY_ENABLED": False,
        **values,
    }


async def _verify_dummy(cache_dir: Path, engine: str) -> None:
    first_downloader = RecordingDownloader()
    crawler = Crawler(
        CacheSpider,
        _settings(cache_dir, engine),
        downloader=first_downloader,
    )
    first = await crawler.crawl()
    assert len(first_downloader.requests) == 1
    assert first.items[0]["flags"] == ()
    assert first.stats["httpcache/miss"] == 1
    assert first.stats["httpcache/firsthand"] == 1
    assert first.stats["httpcache/store"] == 1
    assert crawler.engine is not None
    cache_middleware = next(
        middleware
        for middleware in crawler.engine.downloader_middleware.middleware
        if isinstance(middleware, HttpCacheMiddleware)
    )
    assert cache_middleware.storage.store is None

    cached_downloader = RecordingDownloader(body=b"must not be downloaded")
    cached = await Crawler(
        CacheSpider,
        _settings(cache_dir, engine),
        downloader=cached_downloader,
    ).crawl()
    assert cached_downloader.requests == []
    assert cached.items == (
        {
            "url": "https://example.test/final",
            "body": "<html>live</html>",
            "flags": ("cached",),
            "headers": [b"one", b"two"],
        },
    )
    assert cached.stats["httpcache/hit"] == 1

    bypass_downloader = RecordingDownloader(body=b"<html>bypass</html>")
    bypass = await Crawler(
        CacheSpider,
        _settings(cache_dir, engine),
        downloader=bypass_downloader,
    ).crawl(meta={"dont_cache": True})
    assert len(bypass_downloader.requests) == 1
    assert bypass.items[0]["body"] == "<html>bypass</html>"


async def _verify_rfc(cache_dir: Path, engine: str) -> None:
    settings = _settings(
        cache_dir,
        engine,
        HTTPCACHE_POLICY="spideroxide.httpcache.RFC2616Policy",
    )
    initial_downloader = RecordingDownloader(
        body=b"<html>rfc body</html>",
        headers={
            "Content-Type": "text/html; charset=utf-8",
            "Cache-Control": "no-cache",
            "ETag": '"version-1"',
        },
    )
    initial = await Crawler(
        CacheSpider,
        settings,
        downloader=initial_downloader,
    ).crawl()
    assert initial.stats["httpcache/store"] == 1

    validating_downloader = RecordingDownloader(
        status=304,
        body=b"",
        headers={"ETag": '"version-1"'},
    )
    validated = await Crawler(
        CacheSpider,
        settings,
        downloader=validating_downloader,
    ).crawl()
    assert len(validating_downloader.requests) == 1
    assert validating_downloader.requests[0].headers["If-None-Match"] == b'"version-1"'
    assert validated.items[0]["body"] == "<html>rfc body</html>"
    assert validated.items[0]["flags"] == ("cached",)
    assert validated.stats["httpcache/revalidate"] == 1

    no_store_dir = cache_dir / "no-store"
    no_store_settings = _settings(
        no_store_dir,
        engine,
        HTTPCACHE_POLICY="spideroxide.httpcache.RFC2616Policy",
    )
    no_store_downloader = RecordingDownloader(
        headers={
            "Content-Type": "text/html",
            "Cache-Control": "no-store",
        }
    )
    no_store = await Crawler(
        CacheSpider,
        no_store_settings,
        downloader=no_store_downloader,
    ).crawl()
    assert no_store.stats["httpcache/uncacheable"] == 1
    store = NativeHttpCacheStore(str(no_store_dir / CacheSpider.name))
    assert len(store) == 0
    store.close()


def _verify_rfc_policy() -> None:
    now = time.time()
    date = formatdate(now - 20, usegmt=True)
    modified = formatdate(now - 120, usegmt=True)
    settings = Settings(
        {
            "HTTPCACHE_POLICY": "spideroxide.httpcache.RFC2616Policy",
        }
    )
    policy = RFC2616Policy(settings)
    cached = Response(
        "https://example.test/",
        headers={
            "Cache-Control": "max-age=10",
            "Date": date,
            "ETag": '"cached"',
            "Last-Modified": modified,
        },
        body=b"cached",
        flags=("cached",),
    )

    stale_request = Request(
        "https://example.test/",
        headers={
            "Cache-Control": "max-age=0",
            "If-None-Match": '"caller"',
            "If-Modified-Since": formatdate(now, usegmt=True),
        },
    )
    assert policy.is_cached_response_fresh(cached, stale_request) is False
    assert stale_request.headers["If-None-Match"] == b'"cached"'
    assert stale_request.headers["If-Modified-Since"] == modified.encode()

    max_stale = Request(
        "https://example.test/",
        headers={"Cache-Control": "max-stale=20"},
    )
    assert policy.is_cached_response_fresh(cached, max_stale) is True

    must_revalidate = cached.replace(
        headers={
            "Cache-Control": "max-age=10, must-revalidate",
            "Date": date,
            "ETag": '"cached"',
        }
    )
    blocked_stale = Request(
        "https://example.test/",
        headers={"Cache-Control": "max-stale"},
    )
    assert policy.is_cached_response_fresh(must_revalidate, blocked_stale) is False
    assert (
        policy.is_cached_response_valid(
            must_revalidate,
            Response("https://example.test/", status=503),
            blocked_stale,
        )
        is False
    )

    heuristic = Response(
        "https://example.test/",
        headers={"Date": formatdate(now, usegmt=True), "Last-Modified": modified},
    )
    assert policy.is_cached_response_fresh(
        heuristic,
        Request("https://example.test/"),
    )

    ignored_policy = RFC2616Policy(
        Settings({"HTTPCACHE_IGNORE_RESPONSE_CACHE_CONTROLS": ["no-store"]})
    )
    ignored_no_store = Response(
        "https://example.test/",
        headers={"Cache-Control": "no-store", "ETag": '"cacheable"'},
    )
    assert ignored_policy.should_cache_response(
        ignored_no_store,
        Request("https://example.test/"),
    )

    ignored_code_policy = RFC2616Policy(Settings({"HTTPCACHE_IGNORE_HTTP_CODES": [200]}))
    assert ignored_code_policy.should_cache_response(
        Response("https://example.test/", headers={"Cache-Control": "max-age=60"}),
        Request("https://example.test/"),
    )

    for old_date in (
        "Sun Nov  6 08:49:37 1994",
        "Sun, 06 Nov 1994 08:49:37 -0000",
    ):
        assert (
            policy.is_cached_response_fresh(
                Response(
                    "https://example.test/",
                    headers={"Cache-Control": "max-age=60", "Date": old_date},
                ),
                Request("https://example.test/"),
            )
            is False
        )

    assert (
        policy.is_cached_response_fresh(
            Response(
                "https://example.test/",
                headers={"Cache-Control": "max-age=inf", "Date": date},
            ),
            Request("https://example.test/"),
        )
        is False
    )


def _verify_middleware_errors(cache_dir: Path) -> None:
    settings = Settings(
        {
            "HTTPCACHE_ENABLED": True,
            "HTTPCACHE_DIR": str(cache_dir),
        }
    )
    stats = StatsCollector()
    storage = NativeHttpCacheStorage(settings)
    middleware = HttpCacheMiddleware(
        settings,
        stats,
        RFC2616Policy(settings),
        storage,
    )
    request = Request("https://example.test/")
    try:
        middleware.process_request(request, CacheSpider())
    except RuntimeError as error:
        assert str(error) == "HTTP cache storage is not open"
    else:
        raise AssertionError("closed cache retrieval must fail")
    assert stats.get_value("httpcache/retrieve_error") == 1
    assert stats.get_value("httpcache/miss") is None

    storage.open_spider(CacheSpider())
    response = Response("https://example.test/", body=b"live")
    processed = middleware.process_response(request, response, CacheSpider())
    assert processed.headers.get("Date") is not None
    storage.close_spider(CacheSpider())


async def _verify_engine(engine: str) -> None:
    with tempfile.TemporaryDirectory(prefix=f"spideroxide-cache-{engine}-") as directory:
        root = Path(directory)
        await _verify_dummy(root / "dummy", engine)
        await _verify_rfc(root / "rfc", engine)

        missing_downloader = RecordingDownloader()
        missing = await Crawler(
            CacheSpider,
            _settings(
                root / "missing",
                engine,
                HTTPCACHE_IGNORE_MISSING=True,
            ),
            downloader=missing_downloader,
        ).crawl()
        assert missing.items == ()
        assert missing_downloader.requests == []
        assert missing.stats["httpcache/miss"] == 1
        assert missing.stats["httpcache/ignore"] == 1

        store = NativeHttpCacheStore(str(root / "expiration"))
        store.store(b"key", 0.0, "https://example.test/", 200, [], b"body")
        assert store.retrieve(b"key", 10.0, 1) is None
        assert len(store) == 0
        store.close()

        _verify_middleware_errors(root / "errors")


async def _verify() -> None:
    _verify_rfc_policy()
    for engine in ("python", "rust"):
        await _verify_engine(engine)


if __name__ == "__main__":
    asyncio.run(_verify())
    print(
        "HTTP cache passed: native persistence, hits, misses, bypass, expiration, "
        "RFC freshness, revalidation, lifecycle, failures, repeated headers, and engine parity"
    )
