from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from spideroxide._native import NativePolicyRuntime

from spideroxide import (
    BackendUnavailableError,
    Crawler,
    Request,
    Response,
    RetryMiddleware,
    Spider,
    get_retry_request,
)


class PolicyDownloader:
    def __init__(self, statuses: list[int]) -> None:
        self.statuses = statuses
        self.history: list[Request] = []
        self.closed = False

    async def fetch(self, request: Request) -> Response:
        status = self.statuses[min(len(self.history), len(self.statuses) - 1)]
        self.history.append(request)
        return Response(request.url, status=status, request=request)

    async def close(self) -> None:
        self.closed = True


class NativePolicySpider(Spider):
    name = "native-policy"
    start_urls = ["https://example.test/policy"]

    def parse(self, response: Response) -> dict[str, int]:
        assert response.request is not None
        return {
            "status": response.status,
            "retry_times": int(response.request.meta.get("retry_times", 0)),
            "priority": response.request.priority,
        }


class NativeManualRetrySpider(Spider):
    name = "native-manual-policy"
    start_urls = ["https://example.test/manual"]

    def parse(self, response: Response) -> Request | dict[str, int]:
        assert response.request is not None
        retry_times = int(response.request.meta.get("retry_times", 0))
        if retry_times == 0:
            retry = get_retry_request(
                response.request,
                spider=self,
                reason="application retry",
                max_retry_times=1,
                priority_adjust=10**50,
                stats_base_key="application_retry",
            )
            assert retry is not None
            return retry
        return {
            "retry_times": retry_times,
            "priority": response.request.priority,
        }


class AdditiveStatsMiddleware:
    @classmethod
    def from_crawler(cls, crawler: Crawler) -> AdditiveStatsMiddleware:
        middleware = cls()
        middleware.crawler = crawler
        return middleware

    def process_request(self, request: Request, spider: Spider) -> None:
        self.crawler.stats.inc_value("downloader/request_count", 100)
        return None


class NativeOnlyUnavailableMiddleware:
    @classmethod
    def from_crawler(cls, crawler: Crawler) -> NativeOnlyUnavailableMiddleware:
        if crawler.native_policy_runtime is not None:
            raise BackendUnavailableError("native fixture is unavailable")
        return cls()


class CustomRetryMiddleware(RetryMiddleware):
    def __init__(self, settings) -> None:
        super().__init__(settings)
        self.retry_http_codes = {418}
        self.exceptions_to_retry = ()


def _verify_direct_runtime() -> None:
    runtime = NativePolicyRuntime([429, 503])
    assert runtime.backend_name == "rust"
    assert runtime.should_retry_status(503) is True
    assert runtime.should_retry_status(404) is False
    assert runtime.should_retry_status(418, [418]) is True
    assert runtime.should_retry_exception(True)
    assert not runtime.should_retry_exception(False)

    runtime.record_request("post")
    runtime.record_response(503)
    runtime.record_exception("example.TransientError")
    adjustment = 10**80
    decision = runtime.retry(
        "0",
        "1",
        str(adjustment),
        "503 Service Unavailable",
        "retry",
    )
    assert decision is not None
    assert decision.retry_times == "1"
    assert int(decision.priority_adjust) == adjustment
    assert (
        runtime.retry(
            decision.retry_times,
            "1",
            decision.priority_adjust,
            "503 Service Unavailable",
            "retry",
        )
        is None
    )

    stats = runtime.snapshot_stats()
    assert stats == {
        "downloader/request_count": 1,
        "downloader/request_method_count/POST": 1,
        "downloader/response_count": 1,
        "downloader/response_status_count/503": 1,
        "downloader/exception_count": 1,
        "downloader/exception_type_count/example.TransientError": 1,
        "retry/count": 1,
        "retry/reason_count/503 Service Unavailable": 1,
        "retry/max_reached": 1,
    }
    assert runtime.drain_stats() == stats
    assert runtime.drain_stats() == {}
    assert runtime.snapshot_stats() == stats

    try:
        runtime.retry("0", "-1", "0", "invalid", "retry")
    except ValueError as error:
        assert "cannot be negative" in str(error)
    else:
        raise AssertionError("native policy accepted a negative retry limit")

    assert runtime.retry("2", "1", "invalid", "exhausted", "retry") is None


async def _verify_engine_integration() -> None:
    downloader = PolicyDownloader([503, 200])
    crawler = Crawler(
        NativePolicySpider,
        {
            "CONCURRENT_REQUESTS": 1,
            "ENGINE_BACKEND": "rust",
            "RETRY_PRIORITY_ADJUST": -7,
        },
        downloader=downloader,
    )
    result = await crawler.crawl()
    assert crawler.native_policy_runtime is not None
    assert crawler.native_policy_runtime.backend_name == "rust"
    assert downloader.closed is True
    assert result.items == ({"status": 200, "retry_times": 1, "priority": -7},)
    assert result.stats["retry/count"] == 1
    assert result.stats["downloader/request_count"] == 2
    assert result.stats["downloader/response_count"] == 2
    assert result.stats["downloader/response_status_count/503"] == 1
    assert result.stats["downloader/response_status_count/200"] == 1
    assert crawler.native_policy_runtime.snapshot_stats()["retry/count"] == 1

    for engine in ("python", "rust"):
        additive_crawler = Crawler(
            NativePolicySpider,
            {
                "ENGINE_BACKEND": engine,
                "DOWNLOADER_MIDDLEWARES": {
                    AdditiveStatsMiddleware: 900,
                },
            },
            downloader=PolicyDownloader([200]),
        )
        additive_result = await additive_crawler.crawl()
        assert additive_result.stats["downloader/request_count"] == 101


async def _verify_disabled_policy_and_fallback_cleanup() -> None:
    for engine in ("rust", "auto"):
        crawler = Crawler(
            NativePolicySpider,
            {
                "ENGINE_BACKEND": engine,
                "RETRY_EXCEPTIONS": ["optional_package.errors.Missing"],
                "DOWNLOADER_MIDDLEWARES": {
                    "spideroxide.retry.RetryMiddleware": None,
                },
            },
            downloader=PolicyDownloader([200]),
        )
        result = await crawler.crawl()
        assert result.items[0]["status"] == 200

    subclass_downloader = PolicyDownloader([418, 200])
    subclass_crawler = Crawler(
        NativePolicySpider,
        {
            "CONCURRENT_REQUESTS": 1,
            "ENGINE_BACKEND": "rust",
            "DOWNLOADER_MIDDLEWARES": {
                "spideroxide.retry.RetryMiddleware": None,
                CustomRetryMiddleware: 550,
            },
        },
        downloader=subclass_downloader,
    )
    subclass_result = await subclass_crawler.crawl()
    assert len(subclass_downloader.history) == 2
    assert subclass_result.items[0]["status"] == 200
    assert subclass_result.stats["retry/count"] == 1

    large_status_downloader = PolicyDownloader([503, 200])
    large_status_crawler = Crawler(
        NativePolicySpider,
        {
            "CONCURRENT_REQUESTS": 1,
            "ENGINE_BACKEND": "rust",
            "RETRY_HTTP_CODES": [10**100, 503],
        },
        downloader=large_status_downloader,
    )
    large_status_result = await large_status_crawler.crawl()
    assert len(large_status_downloader.history) == 2
    assert large_status_result.items[0]["status"] == 200

    fallback = Crawler(
        NativePolicySpider,
        {
            "ENGINE_BACKEND": "auto",
            "DOWNLOADER_MIDDLEWARES": {
                NativeOnlyUnavailableMiddleware: 600,
            },
        },
        downloader=PolicyDownloader([200]),
    )
    await fallback.crawl()
    assert fallback.engine is not None
    assert fallback.engine.backend_name == "python"
    assert fallback.native_policy_runtime is None


async def _verify_manual_helper_integration() -> None:
    downloader = PolicyDownloader([200, 200])
    crawler = Crawler(
        NativeManualRetrySpider,
        {
            "CONCURRENT_REQUESTS": 1,
            "ENGINE_BACKEND": "rust",
        },
        downloader=downloader,
    )
    result = await crawler.crawl()
    assert result.items == ({"retry_times": 1, "priority": 10**50},)
    assert result.stats["application_retry/count"] == 1
    assert result.stats["application_retry/reason_count/application retry"] == 1
    assert crawler.native_policy_runtime is not None
    native_stats = crawler.native_policy_runtime.snapshot_stats()
    assert native_stats["application_retry/count"] == 1


async def _run_async_checks() -> None:
    await _verify_engine_integration()
    await _verify_disabled_policy_and_fallback_cleanup()
    await _verify_manual_helper_integration()


def run_native_policy_checks() -> None:
    _verify_direct_runtime()
    asyncio.run(_run_async_checks())


if __name__ == "__main__":
    run_native_policy_checks()
    print(
        "Native policy passed: retry matching, arbitrary priorities, attempt stats, "
        "engine integration, and helper integration"
    )
