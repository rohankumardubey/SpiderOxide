from __future__ import annotations

import asyncio
import sys
from collections.abc import Mapping
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from spideroxide._native import NativeDepthPolicy

from spideroxide import Crawler, DepthMiddleware, Request, Response, Spider, StatsCollector


class DepthDownloader:
    def __init__(self) -> None:
        self.history: list[Request] = []
        self.closed = False

    async def fetch(self, request: Request) -> Response:
        self.history.append(request)
        return Response(request.url, request=request)

    async def close(self) -> None:
        self.closed = True


class DepthSpider(Spider):
    name = "depth"

    def __init__(
        self,
        *,
        start_meta: Mapping[str, object] | None = None,
        start_priority: int = 100,
        last_page: int = 3,
    ) -> None:
        super().__init__()
        self.start_meta = dict(start_meta or {})
        self.start_priority = start_priority
        self.last_page = last_page

    async def start(self):
        yield Request(
            "https://example.test/0",
            callback=self.parse_page,
            meta=self.start_meta,
            priority=self.start_priority,
        )

    def parse_page(self, response: Response) -> list[object]:
        assert response.request is not None
        page = int(urlsplit(response.url).path.strip("/"))
        outputs: list[object] = [
            {
                "page": page,
                "depth": response.meta.get("depth", 0),
                "priority": response.request.priority,
            }
        ]
        if page < self.last_page:
            outputs.append(
                response.follow(
                    f"/{page + 1}",
                    callback=self.parse_page,
                    priority=response.request.priority,
                )
            )
        return outputs


def _verify_direct_runtime() -> None:
    policy = NativeDepthPolicy("2", "3", True)
    assert policy.backend_name == "rust"
    policy.record_initial()

    first = policy.process("0", "100")
    assert first.accepted is True
    assert first.depth == "1"
    assert first.priority == "97"

    second = policy.process(first.depth, first.priority)
    assert second.accepted is True
    assert second.depth == "2"
    assert second.priority == "91"

    filtered = policy.process(second.depth, second.priority)
    assert filtered.accepted is False
    assert filtered.depth == "3"
    assert filtered.priority == "82"
    assert policy.snapshot_counts() == {
        "request_depth_count/0": 1,
        "request_depth_count/1": 1,
        "request_depth_count/2": 1,
    }
    assert policy.max_depth_seen() == "2"
    drained = policy.drain_counts()
    assert drained == policy.snapshot_counts()
    assert policy.drain_counts() == {}
    assert policy.drain_max_depth() == "2"
    assert policy.drain_max_depth() is None

    huge = 10**80
    arbitrary = NativeDepthPolicy("0", str(huge), False)
    decision = arbitrary.process(str(huge), str(huge))
    assert decision.accepted is True
    assert int(decision.depth) == huge + 1
    assert int(decision.priority) == huge - (huge + 1) * huge
    assert arbitrary.max_depth_seen() == str(huge + 1)

    for arguments in (("invalid", "0", False), ("0", "invalid", False)):
        try:
            NativeDepthPolicy(*arguments)
        except ValueError:
            pass
        else:
            raise AssertionError("native depth policy accepted a non-integer setting")

    try:
        arbitrary.process("invalid", "0")
    except ValueError:
        pass
    else:
        raise AssertionError("native depth policy accepted a non-integer request depth")


def _verify_direct_middleware_api() -> None:
    stats = StatsCollector()
    middleware = DepthMiddleware(1, stats, verbose_stats=True, prio=2)
    start = Request("https://example.test/start", priority=10)
    assert middleware.get_processed_request(start, None) is start

    response = Response(start.url, request=start)
    middleware._init_depth(response)
    child = middleware.get_processed_request(
        Request("https://example.test/child", priority=10),
        response,
    )
    assert child is not None
    assert child.meta["depth"] == 1
    assert child.priority == 8

    grandchild = middleware.get_processed_request(
        Request("https://example.test/grandchild", priority=8),
        Response(child.url, request=child),
    )
    assert grandchild is None
    assert stats.get_stats() == {
        "request_depth_count/0": 1,
        "request_depth_count/1": 1,
        "request_depth_max": 1,
    }


async def _crawl(
    engine: str,
    settings: Mapping[str, object] | None = None,
    **spider_kwargs: object,
) -> tuple[Crawler, DepthDownloader, object]:
    downloader = DepthDownloader()
    crawler = Crawler(
        DepthSpider,
        {
            "CONCURRENT_REQUESTS": 1,
            "ENGINE_BACKEND": engine,
            **dict(settings or {}),
        },
        downloader=downloader,
    )
    result = await crawler.crawl(**spider_kwargs)
    assert downloader.closed is True
    return crawler, downloader, result


async def _verify_limits_priorities_and_stats() -> None:
    for engine in ("python", "rust"):
        crawler, downloader, result = await _crawl(
            engine,
            {
                "DEPTH_LIMIT": 2,
                "DEPTH_PRIORITY": 3,
                "DEPTH_STATS_VERBOSE": True,
            },
        )
        assert result.items == (
            {"page": 0, "depth": 0, "priority": 100},
            {"page": 1, "depth": 1, "priority": 97},
            {"page": 2, "depth": 2, "priority": 91},
        )
        assert [request.meta["depth"] for request in downloader.history] == [0, 1, 2]
        assert [request.priority for request in downloader.history] == [100, 97, 91]
        assert result.stats["request_depth_count/0"] == 1
        assert result.stats["request_depth_count/1"] == 1
        assert result.stats["request_depth_count/2"] == 1
        assert "request_depth_count/3" not in result.stats
        assert result.stats["request_depth_max"] == 2

        if engine == "rust":
            assert crawler.native_depth_policy is not None
            assert crawler.native_depth_policy.backend_name == "rust"
            assert crawler.native_depth_policy.max_depth_seen() == "2"
        else:
            assert crawler.native_depth_policy is None

        _, unlimited_downloader, unlimited = await _crawl(
            engine,
            {"DEPTH_LIMIT": 0, "DEPTH_PRIORITY": -2},
        )
        assert [request.priority for request in unlimited_downloader.history] == [
            100,
            102,
            106,
            112,
        ]
        assert unlimited.stats["request_depth_max"] == 3
        assert not any(key.startswith("request_depth_count/") for key in unlimited.stats)

        _, negative_downloader, negative = await _crawl(
            engine,
            {"DEPTH_LIMIT": -1},
        )
        assert len(negative_downloader.history) == 1
        assert negative.items == ({"page": 0, "depth": 0, "priority": 100},)
        assert "request_depth_max" not in negative.stats


async def _verify_custom_depth_and_disabling() -> None:
    huge = 10**60
    for engine in ("python", "rust"):
        _, downloader, result = await _crawl(
            engine,
            {
                "DEPTH_LIMIT": 0,
                "DEPTH_PRIORITY": huge,
            },
            start_meta={"depth": huge},
            start_priority=huge,
            last_page=1,
        )
        child = downloader.history[1]
        assert child.meta["depth"] == huge + 1
        assert child.priority == huge - (huge + 1) * huge
        assert result.stats["request_depth_max"] == huge + 1
        assert "request_depth_count/0" not in result.stats

        _, explicit_zero_downloader, explicit_zero = await _crawl(
            engine,
            {
                "DEPTH_STATS_VERBOSE": True,
            },
            start_meta={"depth": 0},
            last_page=1,
        )
        assert [request.meta["depth"] for request in explicit_zero_downloader.history] == [0, 1]
        assert "request_depth_count/0" not in explicit_zero.stats
        assert explicit_zero.stats["request_depth_count/1"] == 1

        crawler, disabled_downloader, disabled = await _crawl(
            engine,
            {
                "DEPTH_LIMIT": 1,
                "SPIDER_MIDDLEWARES": {
                    "spideroxide.depth.DepthMiddleware": None,
                },
            },
        )
        assert len(disabled_downloader.history) == 4
        assert all("depth" not in request.meta for request in disabled_downloader.history)
        assert "request_depth_max" not in disabled.stats
        if engine == "rust":
            assert crawler.native_depth_policy is not None
            assert crawler.native_depth_policy.max_depth_seen() is None

        _, class_disabled_downloader, _ = await _crawl(
            engine,
            {
                "DEPTH_LIMIT": 1,
                "SPIDER_MIDDLEWARES": {DepthMiddleware: None},
            },
        )
        assert len(class_disabled_downloader.history) == 4


async def _verify_construction_cleanup() -> None:
    for engine in ("python", "rust"):
        downloader = DepthDownloader()
        crawler = Crawler(
            DepthSpider,
            {
                "DEPTH_LIMIT": "invalid",
                "ENGINE_BACKEND": engine,
            },
            downloader=downloader,
        )
        try:
            await crawler.crawl()
        except ValueError:
            pass
        else:
            raise AssertionError(f"{engine} crawler accepted a non-integer DEPTH_LIMIT")
        assert downloader.closed is True


async def _run_async_checks() -> None:
    await _verify_limits_priorities_and_stats()
    await _verify_custom_depth_and_disabling()
    await _verify_construction_cleanup()


def run_depth_checks() -> None:
    _verify_direct_runtime()
    _verify_direct_middleware_api()
    asyncio.run(_run_async_checks())


if __name__ == "__main__":
    run_depth_checks()
    print(
        "Depth policy passed: limits, priorities, stats, arbitrary integers, "
        "middleware disabling, engine parity, and native ownership"
    )
