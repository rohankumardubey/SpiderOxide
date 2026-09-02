from __future__ import annotations

import asyncio
import gc
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from spideroxide._native import NativeDownloadSlotManager  # noqa: E402

from spideroxide import (  # noqa: E402
    Crawler,
    DownloadError,
    Request,
    Response,
    Spider,
    signals,
)


async def _wait_until(predicate, message: str) -> None:
    for _ in range(100):
        if predicate():
            return
        await asyncio.sleep(0.001)
    raise AssertionError(message)


async def _verify_native_manager() -> None:
    try:
        NativeDownloadSlotManager(1, 1e300)
    except ValueError:
        pass
    else:
        raise AssertionError("unrepresentable download delay was accepted")
    try:
        NativeDownloadSlotManager(1, 1.3e19, True)
    except ValueError:
        pass
    else:
        raise AssertionError("unrepresentable randomized delay was accepted")

    disabled = NativeDownloadSlotManager(1, 61.0, False, False, -1.0, 60.0, 0.0)
    lease = await disabled.acquire("slow.test")
    disabled.release(lease)
    assert disabled.prune_inactive(0.0) == 1

    manager = NativeDownloadSlotManager(2, 0.03, False)
    first = await manager.acquire("example.test")
    started = asyncio.get_running_loop().time()
    second = await manager.acquire("example.test")
    elapsed = asyncio.get_running_loop().time() - started
    assert elapsed >= 0.02
    assert manager.slot_state("example.test")[:2] == (2, 2)

    blocked = asyncio.ensure_future(manager.acquire("example.test"))
    await asyncio.sleep(0.01)
    assert not blocked.done()
    other = await manager.acquire("other.test")
    manager.release(other)
    manager.release(first)
    third = await asyncio.wait_for(blocked, 1.0)
    manager.release(second)
    manager.release(third)
    assert manager.stats() == {
        "downloader/slot/acquired": 4,
        "downloader/slot/released": 4,
    }
    assert manager.drain_stats() == manager.stats()
    assert manager.drain_stats() == {}

    throttled = NativeDownloadSlotManager(1, 0.1, False, True, 0.2, 2.0, 2.0)
    lease = await throttled.acquire("api.test")
    throttled.release(lease, 1.0, 200)
    assert throttled.slot_state("api.test")[2:] == (0.5, 1.0)

    lease = await throttled.acquire("api.test")
    throttled.release(lease, 0.1, 500)
    assert throttled.slot_state("api.test")[2] == 0.5

    lease = await throttled.acquire("api.test")
    throttled.release(lease, 0.1, 200, False)
    assert throttled.slot_state("api.test")[2] == 0.5
    assert throttled.stats()["autothrottle/increased"] == 1
    assert throttled.stats()["autothrottle/ignored"] == 1

    active = await throttled.acquire("blocked.test", 1, 10.0, False)
    waiting = asyncio.ensure_future(throttled.acquire("blocked.test", 1, 10.0, False))
    await _wait_until(
        lambda: throttled.waiting_count("blocked.test") == 1,
        "blocked acquisition did not register as a waiter",
    )
    assert throttled.prune_inactive(0.0) == 1
    throttled.release(active)
    assert throttled.waiting_count("blocked.test") == 1
    assert throttled.prune_inactive(0.0) == 0
    waiting.cancel()
    await asyncio.gather(waiting, return_exceptions=True)
    await _wait_until(
        lambda: throttled.waiting_count("blocked.test") == 0,
        "cancelled acquisition remained registered as a waiter",
    )
    assert throttled.prune_inactive(0.0) == 1

    active = await throttled.acquire("closing.test")
    waiting = asyncio.ensure_future(throttled.acquire("closing.test"))
    await _wait_until(
        lambda: throttled.waiting_count("closing.test") == 1,
        "closing acquisition did not register as a waiter",
    )
    throttled.close()
    try:
        await waiting
    except RuntimeError as error:
        assert "closed" in str(error)
    else:
        raise AssertionError("closing the manager did not wake a blocked acquisition")
    assert throttled.slot_count == 0
    try:
        throttled.release(active)
    except ValueError as error:
        assert "unknown download slot" in str(error)

    cancellation_safe = NativeDownloadSlotManager(1)
    acquired = asyncio.Event()

    async def acquire_until_cancelled() -> None:
        lease = await cancellation_safe.acquire("cancel.test")
        assert lease.key == "cancel.test"
        acquired.set()
        await asyncio.sleep(10)

    cancelled = asyncio.create_task(acquire_until_cancelled())
    await asyncio.wait_for(acquired.wait(), 1.0)
    cancelled.cancel()
    await asyncio.gather(cancelled, return_exceptions=True)
    del cancelled
    gc.collect()
    await _wait_until(
        lambda: cancellation_safe.slot_state("cancel.test")[1] == 0,
        "cancelled task did not release its native lease",
    )
    replacement = await asyncio.wait_for(
        cancellation_safe.acquire("cancel.test"),
        1.0,
    )
    cancellation_safe.release(replacement)
    assert cancellation_safe.stats()["downloader/slot/cancelled"] == 1


class SlotDownloader:
    def __init__(
        self,
        delay: float = 0.025,
        reported_latency: float | None = None,
    ) -> None:
        self.delay = delay
        self.reported_latency = reported_latency
        self.active: defaultdict[str, int] = defaultdict(int)
        self.maximum: defaultdict[str, int] = defaultdict(int)
        self.starts: defaultdict[str, list[float]] = defaultdict(list)
        self.closed = False

    async def fetch(self, request: Request) -> Response:
        key = str(request.meta["download_slot"])
        if self.reported_latency is not None:
            request.meta["download_latency"] = self.reported_latency
        self.starts[key].append(asyncio.get_running_loop().time())
        self.active[key] += 1
        self.maximum[key] = max(self.maximum[key], self.active[key])
        try:
            await asyncio.sleep(self.delay)
            if request.url.endswith("/failure"):
                raise DownloadError("expected slot failure")
            return Response(request.url, request=request)
        finally:
            self.active[key] -= 1

    async def close(self) -> None:
        self.closed = True


class SlotSpider(Spider):
    name = "native-slots"

    def start_requests(self):
        for index in range(4):
            yield Request(f"https://a.test/{index}", dont_filter=True)
        for index in range(2):
            yield Request(f"https://b.test/{index}", dont_filter=True)
        yield Request(
            "https://custom.test/failure",
            meta={"download_slot": "shared"},
            dont_filter=True,
        )
        yield Request(
            "https://another.test/recovery",
            meta={"download_slot": "shared"},
            dont_filter=True,
        )

    def parse(self, response: Response) -> dict[str, str]:
        return {"url": response.url}


async def _verify_crawler_slots() -> None:
    downloader = SlotDownloader()
    crawler = Crawler(
        SlotSpider,
        {
            "ENGINE_BACKEND": "rust",
            "CONCURRENT_REQUESTS": 8,
            "CONCURRENT_REQUESTS_PER_DOMAIN": 2,
            "DOWNLOAD_DELAY": 0.02,
            "RANDOMIZE_DOWNLOAD_DELAY": False,
            "DOWNLOAD_SLOTS": {"shared": {"concurrency": 1, "delay": 0.0}},
            "RETRY_ENABLED": False,
        },
        downloader=downloader,
    )
    result = await crawler.crawl()
    assert result.reason == "finished"
    assert len(result.items) == 7
    assert downloader.closed
    assert downloader.maximum == {"a.test": 2, "b.test": 2, "shared": 1}
    for key in ("a.test", "b.test"):
        starts = downloader.starts[key]
        assert all(
            later - earlier >= 0.015 for earlier, later in zip(starts, starts[1:], strict=False)
        )
    assert result.stats["downloader/slot/acquired"] == 8
    assert result.stats["downloader/slot/released"] == 8
    assert result.stats["downloader/slot/shared/concurrency"] == 1
    assert result.stats["downloader/slot/a.test/delay"] == 0.02
    assert crawler.native_download_slots is not None


class ThrottleSpider(Spider):
    name = "native-autothrottle"
    start_urls = ["https://throttle.test/one", "https://throttle.test/two"]

    def parse(self, response: Response) -> dict[str, str]:
        return {"url": response.url}


async def _verify_crawler_autothrottle() -> None:
    downloader = SlotDownloader(delay=0.04, reported_latency=0.01)
    result = await Crawler(
        ThrottleSpider,
        {
            "ENGINE_BACKEND": "rust",
            "CONCURRENT_REQUESTS": 2,
            "CONCURRENT_REQUESTS_PER_DOMAIN": 1,
            "DOWNLOAD_DELAY": 0.0,
            "RANDOMIZE_DOWNLOAD_DELAY": False,
            "AUTOTHROTTLE_ENABLED": True,
            "AUTOTHROTTLE_START_DELAY": 0.01,
            "AUTOTHROTTLE_MAX_DELAY": 1.0,
            "AUTOTHROTTLE_TARGET_CONCURRENCY": 0.5,
        },
        downloader=downloader,
    ).crawl()
    starts = downloader.starts["throttle.test"]
    assert len(starts) == 2
    assert starts[1] - starts[0] >= 0.035
    assert result.stats["autothrottle/increased"] >= 1
    assert 0.018 <= result.stats["downloader/slot/throttle.test/delay"] <= 0.025


async def _verify_spider_opened_delay() -> None:
    downloader = SlotDownloader(delay=0.001)
    crawler = Crawler(
        ThrottleSpider,
        {
            "ENGINE_BACKEND": "rust",
            "CONCURRENT_REQUESTS": 2,
            "CONCURRENT_REQUESTS_PER_DOMAIN": 1,
            "DOWNLOAD_DELAY": 0.0,
            "RANDOMIZE_DOWNLOAD_DELAY": False,
        },
        downloader=downloader,
    )

    def set_delay(*, spider: Spider) -> None:
        spider.download_delay = 0.04

    crawler.signals.connect(set_delay, signals.spider_opened)
    result = await crawler.crawl()
    assert result.stats["downloader/slot/throttle.test/delay"] == 0.04
    starts = downloader.starts["throttle.test"]
    assert starts[1] - starts[0] >= 0.035


async def _verify_missing_header_latency() -> None:
    result = await Crawler(
        ThrottleSpider,
        {
            "ENGINE_BACKEND": "rust",
            "CONCURRENT_REQUESTS_PER_DOMAIN": 1,
            "RANDOMIZE_DOWNLOAD_DELAY": False,
            "AUTOTHROTTLE_ENABLED": True,
            "AUTOTHROTTLE_START_DELAY": 0.001,
            "AUTOTHROTTLE_MAX_DELAY": 1.0,
        },
        downloader=SlotDownloader(delay=0.01),
    ).crawl()
    assert not any(name.startswith("autothrottle/") for name in result.stats)
    assert result.stats["downloader/slot/throttle.test/delay"] == 0.001


async def main() -> None:
    await _verify_native_manager()
    await _verify_crawler_slots()
    await _verify_crawler_autothrottle()
    await _verify_spider_opened_delay()
    await _verify_missing_header_latency()
    print("Native download slot verification passed.")


if __name__ == "__main__":
    asyncio.run(main())
