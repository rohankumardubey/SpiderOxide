from __future__ import annotations

import asyncio
import sys
from collections.abc import Mapping
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from spideroxide import (
    Crawler,
    DownloadError,
    Request,
    Response,
    RetryMiddleware,
    Spider,
    get_retry_request,
)


class TransientFixtureError(Exception):
    pass


class SequenceDownloader:
    def __init__(self, outcomes: list[int | Exception]) -> None:
        self.outcomes = outcomes
        self.history: list[Request] = []
        self.closed = False

    async def fetch(self, request: Request) -> Response:
        attempt = len(self.history)
        self.history.append(request)
        outcome = self.outcomes[min(attempt, len(self.outcomes) - 1)]
        if isinstance(outcome, Exception):
            raise outcome
        return Response(request.url, status=outcome, request=request)

    async def close(self) -> None:
        self.closed = True


class RetrySpider(Spider):
    name = "retry"

    def __init__(
        self,
        *,
        request_meta: Mapping[str, object] | None = None,
        priority: int = 0,
    ) -> None:
        super().__init__()
        self.request_meta = dict(request_meta or {})
        self.priority = priority

    async def start(self):
        yield Request(
            "https://example.test/retry",
            callback=self.parse_result,
            errback=self.handle_error,
            meta=self.request_meta,
            priority=self.priority,
        )

    def parse_result(self, response: Response) -> dict[str, object]:
        assert response.request is not None
        return {
            "status": response.status,
            "retry_times": response.request.meta.get("retry_times", 0),
            "priority": response.request.priority,
        }

    def handle_error(self, exception: BaseException) -> dict[str, str]:
        return {"error": str(exception)}


class ManualRetrySpider(Spider):
    name = "manual-retry"
    start_urls = ["https://example.test/manual"]

    def parse(self, response: Response) -> Request | dict[str, int]:
        assert response.request is not None
        retry_times = int(response.request.meta.get("retry_times", 0))
        if retry_times == 0:
            retry = get_retry_request(
                response.request,
                spider=self,
                reason="empty response",
                max_retry_times=1,
                priority_adjust=3,
                stats_base_key="manual_retry",
            )
            assert retry is not None
            return retry
        return {"retry_times": retry_times, "priority": response.request.priority}


async def _crawl(
    outcomes: list[int | Exception],
    *,
    engine: str = "python",
    settings: Mapping[str, object] | None = None,
    request_meta: Mapping[str, object] | None = None,
    priority: int = 0,
) -> tuple[SequenceDownloader, object]:
    downloader = SequenceDownloader(outcomes)
    crawler = Crawler(
        RetrySpider,
        {
            "CONCURRENT_REQUESTS": 1,
            "ENGINE_BACKEND": engine,
            **dict(settings or {}),
        },
        downloader=downloader,
    )
    result = await crawler.crawl(request_meta=request_meta, priority=priority)
    assert downloader.closed is True
    return downloader, result


async def _verify_status_and_exception_parity() -> None:
    for engine in ("python", "rust"):
        status_downloader, status_result = await _crawl(
            [503, 503, 200],
            engine=engine,
            settings={"RETRY_PRIORITY_ADJUST": -2},
            priority=10,
        )
        assert status_result.items == ({"status": 200, "retry_times": 2, "priority": 6},)
        assert [request.priority for request in status_downloader.history] == [10, 8, 6]
        assert [request.meta.get("retry_times", 0) for request in status_downloader.history] == [
            0,
            1,
            2,
        ]
        assert [request.dont_filter for request in status_downloader.history] == [
            False,
            True,
            True,
        ]
        assert status_result.stats["retry/count"] == 2
        assert status_result.stats["retry/reason_count/503 Service Unavailable"] == 2
        assert status_result.stats["downloader/request_count"] == 3
        assert status_result.stats["downloader/response_count"] == 3
        assert status_result.stats["downloader/response_status_count/503"] == 2
        assert status_result.stats["downloader/response_status_count/200"] == 1

        error_downloader, error_result = await _crawl(
            [DownloadError("temporary"), DownloadError("temporary"), 200],
            engine=engine,
        )
        assert error_result.items == ({"status": 200, "retry_times": 2, "priority": -2},)
        assert len(error_downloader.history) == 3
        reason = "retry/reason_count/spideroxide.exceptions.DownloadError"
        assert error_result.stats[reason] == 2
        assert error_result.stats["downloader/exception_count"] == 2
        exception_stat = "downloader/exception_type_count/spideroxide.exceptions.DownloadError"
        assert error_result.stats[exception_stat] == 2


async def _verify_limits_and_opt_outs() -> None:
    exhausted, exhausted_result = await _crawl(
        [500],
        settings={"RETRY_TIMES": 1},
    )
    assert len(exhausted.history) == 2
    assert exhausted_result.items == ({"status": 500, "retry_times": 1, "priority": -1},)
    assert exhausted_result.stats["retry/count"] == 1
    assert exhausted_result.stats["retry/max_reached"] == 1

    failed, failed_result = await _crawl(
        [DownloadError("permanent failure")],
        settings={"RETRY_TIMES": 1},
    )
    assert len(failed.history) == 2
    assert failed_result.items == ({"error": "permanent failure"},)
    assert failed_result.stats["retry/count"] == 1
    assert failed_result.stats["retry/max_reached"] == 1
    assert failed_result.stats["downloader/exception_count"] == 2

    overridden, override_result = await _crawl(
        [503, 503, 503, 200],
        request_meta={
            "max_retry_times": 3,
            "priority_adjust": 5,
            "give_up_log_level": None,
        },
    )
    assert [request.priority for request in overridden.history] == [0, 5, 10, 15]
    assert override_result.items == ({"status": 200, "retry_times": 3, "priority": 15},)

    none_overrides, none_override_result = await _crawl(
        [503, 200],
        request_meta={"max_retry_times": None, "priority_adjust": None},
    )
    assert [request.priority for request in none_overrides.history] == [0, -1]
    assert none_override_result.items[0]["status"] == 200

    dont_retry, dont_retry_result = await _crawl(
        [503],
        request_meta={"dont_retry": True},
    )
    assert len(dont_retry.history) == 1
    assert dont_retry_result.items == ({"status": 503, "retry_times": 0, "priority": 0},)
    assert "retry/count" not in dont_retry_result.stats

    disabled, disabled_result = await _crawl(
        [503],
        settings={"RETRY_ENABLED": False},
    )
    assert len(disabled.history) == 1
    assert disabled_result.items[0]["status"] == 503

    removed, removed_result = await _crawl(
        [503],
        settings={
            "DOWNLOADER_MIDDLEWARES": {
                "spideroxide.retry.RetryMiddleware": None,
            }
        },
    )
    assert len(removed.history) == 1
    assert removed_result.items[0]["status"] == 503

    class_removed, class_removed_result = await _crawl(
        [503],
        settings={"DOWNLOADER_MIDDLEWARES": {RetryMiddleware: None}},
    )
    assert len(class_removed.history) == 1
    assert class_removed_result.items[0]["status"] == 503

    missing_disabled, missing_disabled_result = await _crawl(
        [200],
        settings={
            "DOWNLOADER_MIDDLEWARES": {
                "optional_package.middleware.DoesNotExist": None,
            }
        },
    )
    assert len(missing_disabled.history) == 1
    assert missing_disabled_result.items[0]["status"] == 200

    deduplicated, deduplicated_result = await _crawl(
        [503],
        settings={
            "DOWNLOADER_MIDDLEWARES": [RetryMiddleware],
            "RETRY_TIMES": 0,
        },
    )
    assert len(deduplicated.history) == 1
    assert deduplicated_result.stats["retry/max_reached"] == 1


async def _verify_custom_exception_and_helper() -> None:
    custom, custom_result = await _crawl(
        [TransientFixtureError("temporary"), 200],
        settings={"RETRY_EXCEPTIONS": [TransientFixtureError]},
    )
    assert len(custom.history) == 2
    reason = f"retry/reason_count/{__name__}.TransientFixtureError"
    assert custom_result.stats[reason] == 1

    helper_downloader = SequenceDownloader([200])
    helper_result = await Crawler(
        ManualRetrySpider,
        {"CONCURRENT_REQUESTS": 1},
        downloader=helper_downloader,
    ).crawl()
    assert helper_result.items == ({"retry_times": 1, "priority": 3},)
    assert helper_result.stats["manual_retry/count"] == 1
    assert helper_result.stats["manual_retry/reason_count/empty response"] == 1


async def _verify_construction_cleanup() -> None:
    downloader = SequenceDownloader([200])
    crawler = Crawler(
        RetrySpider,
        {"RETRY_TIMES": -1},
        downloader=downloader,
    )
    try:
        await crawler.crawl()
    except ValueError as error:
        assert "RETRY_TIMES" in str(error)
    else:
        raise AssertionError("crawler accepted negative RETRY_TIMES")
    assert downloader.closed is True


def _verify_invalid_settings() -> None:
    try:
        from spideroxide import Settings

        RetryMiddleware(Settings({"RETRY_TIMES": -1}))
    except ValueError as error:
        assert "RETRY_TIMES" in str(error)
    else:
        raise AssertionError("negative RETRY_TIMES was accepted")

    try:
        RetryMiddleware(Settings({"RETRY_EXCEPTIONS": [object]}))
    except TypeError as error:
        assert "exception classes" in str(error)
    else:
        raise AssertionError("non-exception RETRY_EXCEPTIONS entry was accepted")


async def _run_async_checks() -> None:
    await _verify_status_and_exception_parity()
    await _verify_limits_and_opt_outs()
    await _verify_custom_exception_and_helper()
    await _verify_construction_cleanup()


def run_retry_checks() -> None:
    _verify_invalid_settings()
    asyncio.run(_run_async_checks())


if __name__ == "__main__":
    run_retry_checks()
    print(
        "Retry policy passed: statuses, exceptions, limits, overrides, stats, "
        "helper API, and engine parity"
    )
