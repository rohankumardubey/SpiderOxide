from __future__ import annotations

import asyncio
import sys
from collections import Counter
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from spideroxide._native import NativeRobotsRuntime  # noqa: E402

from spideroxide import (  # noqa: E402
    BackendUnavailableError,
    Crawler,
    DownloadError,
    Headers,
    Request,
    Response,
    Spider,
)


async def _verify_runtime() -> None:
    edge_runtime = NativeRobotsRuntime()
    bom = edge_runtime.check("https://bom.test/private", "SpiderOxide")
    edge_runtime.complete(
        bom.origin,
        200,
        b"\xef\xbb\xbfUser-agent: *\nDisallow: /private",
    )
    assert edge_runtime.check("https://bom.test/private", "SpiderOxide").action == "deny"
    unicode_policy = edge_runtime.check("https://unicode.test/start", "SpiderOxide")
    edge_runtime.complete(
        unicode_policy.origin,
        200,
        b"User-agent: *\nDisallow: /%E7%A7%98%E5%AF%86",
    )
    assert edge_runtime.check("https://unicode.test/\u79d8\u5bc6", "SpiderOxide").action == "deny"
    edge_runtime.close()

    runtime = NativeRobotsRuntime()
    first = runtime.check("https://example.test/private", "SpiderOxide")
    assert first.action == "fetch"
    assert first.origin == "https://example.test"
    assert first.robots_url == "https://example.test/robots.txt"
    ipv6 = runtime.check("http://[::1]:8080/page", "SpiderOxide")
    assert ipv6.origin == "http://[::1]:8080"
    assert ipv6.robots_url == "http://[::1]:8080/robots.txt"
    runtime.fail(ipv6.origin, "unreachable")

    waiting = runtime.check("https://example.test/public", "SpiderOxide")
    assert waiting.action == "wait"
    waiter = asyncio.ensure_future(runtime.wait(waiting.origin))
    await asyncio.sleep(0)
    assert not waiter.done()

    runtime.complete(
        first.origin,
        200,
        b"""
        User-agent: SpiderOxide
        Disallow: /private
        Allow: /private/open

        User-agent: *
        Disallow: /fallback
        """,
    )
    assert await waiter
    assert runtime.check("https://example.test/public", "SpiderOxide").action == "allow"
    assert runtime.check("https://example.test/private", "SpiderOxide").action == "deny"
    assert runtime.check("https://example.test/private/open", "SpiderOxide").action == "allow"
    assert runtime.check("https://example.test/fallback", "OtherBot").action == "deny"
    assert runtime.origin_count == 2

    failed = runtime.check("http://failed.test/page", "SpiderOxide")
    runtime.fail(failed.origin, "spideroxide.exceptions.DownloadError")
    assert runtime.check("http://failed.test/page", "SpiderOxide").action == "allow"
    runtime.record_bypass()
    assert runtime.stats() == {
        "robotstxt/allowed": 3,
        "robotstxt/bypassed": 1,
        "robotstxt/exception_count/spideroxide.exceptions.DownloadError": 1,
        "robotstxt/exception_count/unreachable": 1,
        "robotstxt/forbidden": 2,
        "robotstxt/request_count": 3,
        "robotstxt/response_count": 1,
        "robotstxt/response_status_count/200": 1,
    }
    assert runtime.drain_stats() == runtime.stats()
    assert runtime.drain_stats() == {}

    closing = runtime.check("https://closing.test/page", "SpiderOxide")
    blocked = asyncio.ensure_future(runtime.wait(closing.origin))
    await asyncio.sleep(0)
    runtime.close()
    assert await blocked is False
    assert runtime.origin_count == 0


class RobotsDownloader:
    def __init__(self) -> None:
        self.history: list[Request] = []
        self.closed = False

    async def fetch(self, request: Request) -> Response:
        self.history.append(request)
        parsed = urlsplit(request.url)
        if parsed.path == "/robots.txt":
            await asyncio.sleep(0.01)
            if parsed.hostname == "failed.test":
                raise DownloadError("robots unavailable")
            if parsed.hostname == "redirect.test":
                return Response(
                    request.url,
                    status=302,
                    headers=Headers({"Location": "https://policy.test/redirect-robots"}),
                    request=request,
                )
            if parsed.hostname == "example.test":
                body = b"""
                    User-agent: SpiderOxide
                    Disallow: /private
                    Allow: /private/open

                    User-agent: SpecialBot
                    Disallow: /header-blocked
                """
            elif parsed.hostname == "override.test":
                body = b"""
                    User-agent: ConfiguredBot
                    Disallow: /blocked

                    User-agent: HeaderBot
                    Allow: /
                """
            elif parsed.hostname == "status.test":
                return Response(
                    request.url,
                    status=404,
                    body=b"User-agent: *\nDisallow: /blocked",
                    request=request,
                )
            else:
                body = b"User-agent: *\nAllow: /"
            return Response(request.url, body=body, request=request)
        if parsed.hostname == "policy.test" and parsed.path == "/redirect-robots":
            return Response(
                request.url,
                body=b"User-agent: *\nDisallow: /blocked",
                request=request,
            )
        return Response(request.url, request=request)

    async def close(self) -> None:
        self.closed = True


class RobotsSpider(Spider):
    name = "native-robots"

    def start_requests(self):
        yield Request("https://example.test/public", dont_filter=True)
        yield Request("https://example.test/private", dont_filter=True)
        yield Request("https://example.test/private/open", dont_filter=True)
        yield Request(
            "https://example.test/header-blocked",
            headers=Headers({"User-Agent": "SpecialBot"}),
            dont_filter=True,
        )
        yield Request(
            "https://example.test/private",
            meta={"dont_obey_robotstxt": True},
            dont_filter=True,
        )
        yield Request("https://failed.test/one", dont_filter=True)
        yield Request("https://failed.test/two", dont_filter=True)
        yield Request("https://redirect.test/blocked", dont_filter=True)

    def parse(self, response: Response) -> dict[str, str]:
        return {"url": response.url}


async def _verify_crawler() -> None:
    downloader = RobotsDownloader()
    crawler = Crawler(
        RobotsSpider,
        {
            "ENGINE_BACKEND": "rust",
            "CONCURRENT_REQUESTS": 8,
            "CONCURRENT_REQUESTS_PER_DOMAIN": 2,
            "ROBOTSTXT_OBEY": True,
            "RETRY_TIMES": 1,
        },
        downloader=downloader,
    )
    result = await crawler.crawl()
    assert result.reason == "finished"
    assert downloader.closed
    assert {item["url"] for item in result.items} == {
        "https://example.test/public",
        "https://example.test/private/open",
        "https://example.test/private",
        "https://failed.test/one",
        "https://failed.test/two",
    }

    counts = Counter(request.url for request in downloader.history)
    assert counts["https://example.test/robots.txt"] == 1
    assert counts["https://failed.test/robots.txt"] == 2
    assert counts["https://redirect.test/robots.txt"] == 1
    assert counts["https://policy.test/redirect-robots"] == 1
    assert counts["https://example.test/private"] == 1
    assert counts["https://example.test/header-blocked"] == 0
    assert counts["https://redirect.test/blocked"] == 0

    assert result.stats["robotstxt/request_count"] == 3
    assert result.stats["robotstxt/response_count"] == 2
    assert result.stats["robotstxt/response_status_count/200"] == 2
    assert result.stats["robotstxt/forbidden"] == 3
    assert result.stats["robotstxt/allowed"] == 4
    assert result.stats["robotstxt/bypassed"] == 1
    assert result.stats["robotstxt/exception_count/spideroxide.exceptions.DownloadError"] == 1
    assert crawler.native_robots_runtime is not None


class OverrideSpider(Spider):
    name = "robots-user-agent-override"

    def start_requests(self):
        yield Request(
            "https://override.test/blocked",
            headers=Headers({"User-Agent": "HeaderBot/1.0"}),
        )
        yield Request("https://status.test/blocked")

    def parse(self, response: Response) -> dict[str, str]:
        return {"url": response.url}


async def _verify_user_agent_and_status() -> None:
    downloader = RobotsDownloader()
    result = await Crawler(
        OverrideSpider,
        {
            "ENGINE_BACKEND": "rust",
            "ROBOTSTXT_OBEY": True,
            "ROBOTSTXT_USER_AGENT": "ConfiguredBot/1.0",
        },
        downloader=downloader,
    ).crawl()
    assert result.items == ()
    assert result.stats["robotstxt/forbidden"] == 2
    assert result.stats["robotstxt/response_status_count/200"] == 1
    assert result.stats["robotstxt/response_status_count/404"] == 1


class BlockingRobotsDownloader:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.closed = False

    async def fetch(self, request: Request) -> Response:
        if request.url.endswith("/robots.txt"):
            self.started.set()
            await asyncio.Event().wait()
        return Response(request.url, request=request)

    async def close(self) -> None:
        self.closed = True


class CancellationSpider(Spider):
    name = "robots-cancellation"
    start_urls = [
        "https://cancel.test/one",
        "https://cancel.test/two",
    ]

    def parse(self, response: Response) -> None:
        return None


async def _verify_cancellation() -> None:
    downloader = BlockingRobotsDownloader()
    crawler = Crawler(
        CancellationSpider,
        {
            "ENGINE_BACKEND": "rust",
            "CONCURRENT_REQUESTS": 2,
            "ROBOTSTXT_OBEY": True,
        },
        downloader=downloader,
    )
    crawl = asyncio.create_task(crawler.crawl())
    await asyncio.wait_for(downloader.started.wait(), 1)
    crawl.cancel()
    try:
        await crawl
    except asyncio.CancelledError:
        pass
    else:
        raise AssertionError("robots crawl cancellation was swallowed")
    assert downloader.closed
    assert crawler.native_robots_runtime is not None
    assert crawler.native_robots_runtime.origin_count == 0


class ClosedDownloader:
    def __init__(self) -> None:
        self.closed = False

    async def fetch(self, request: Request) -> Response:
        return Response(request.url, request=request)

    async def close(self) -> None:
        self.closed = True


async def _verify_native_requirement() -> None:
    downloader = ClosedDownloader()
    try:
        await Crawler(
            RobotsSpider,
            {
                "ENGINE_BACKEND": "python",
                "ROBOTSTXT_OBEY": True,
            },
            downloader=downloader,
        ).crawl()
    except BackendUnavailableError as error:
        assert "Rust engine" in str(error)
    else:
        raise AssertionError("Python engine accepted native robots policy")
    assert downloader.closed


async def main() -> None:
    await _verify_runtime()
    await _verify_crawler()
    await _verify_user_agent_and_status()
    await _verify_cancellation()
    await _verify_native_requirement()
    print("Native robots policy verification passed.")


if __name__ == "__main__":
    asyncio.run(main())
