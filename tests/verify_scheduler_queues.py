from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from spideroxide._native import NativeCrawlCoordinator

from spideroxide import Crawler, Request, Response, Spider


class RecordingDownloader:
    def __init__(self) -> None:
        self.urls: list[str] = []

    async def fetch(self, request: Request) -> Response:
        self.urls.append(request.url)
        return Response(request.url, request=request)

    async def close(self) -> None:
        pass


class QueueSpider(Spider):
    name = "scheduler-queues"

    async def start(self):
        yield Request(
            "https://example.test/start-1",
            callback=self.parse_root,
            dont_filter=True,
        )
        yield Request(
            "https://example.test/start-2",
            callback=self.parse_page,
            dont_filter=True,
        )

    def parse_root(self, response: Response) -> list[Request]:
        assert response.request is not None
        assert response.request.meta["is_start_request"] is True
        return [
            Request(
                "https://example.test/normal-1",
                callback=self.parse_page,
                dont_filter=True,
            ),
            Request(
                "https://example.test/normal-2",
                callback=self.parse_page,
                dont_filter=True,
            ),
        ]

    def parse_page(self, response: Response) -> dict[str, str]:
        assert response.request is not None
        expected = response.url.rsplit("/", 1)[-1].startswith("start-")
        assert bool(response.request.meta.get("is_start_request", False)) is expected
        return {"url": response.url}


def _schedule(
    coordinator: NativeCrawlCoordinator,
    name: str,
    *,
    priority: str = "0",
    payload: bytes | None = None,
    is_start: bool = False,
) -> int:
    request_id = coordinator.schedule(
        f"https://example.test/{name}",
        "GET",
        b"",
        priority,
        False,
        payload,
        is_start,
    )
    assert request_id is not None
    coordinator.activate(request_id)
    return request_id


async def _drain(coordinator: NativeCrawlCoordinator) -> list[int]:
    coordinator.close_input()
    output = []
    while (request_id := await coordinator.next_request()) is not None:
        output.append(request_id)
        coordinator.complete(request_id)
    return output


async def _verify_native_queue_modes() -> None:
    coordinator = NativeCrawlCoordinator(
        1,
        10,
        None,
        "lifo",
        "lifo",
        "fifo",
        "fifo",
    )
    normal_one = _schedule(coordinator, "normal-1")
    start_one = _schedule(coordinator, "start-1", is_start=True)
    normal_two = _schedule(coordinator, "normal-2")
    start_two = _schedule(coordinator, "start-2", is_start=True)
    assert await _drain(coordinator) == [
        normal_two,
        normal_one,
        start_one,
        start_two,
    ]
    coordinator.close()

    fifo = NativeCrawlCoordinator(1, 10, None, "fifo", "fifo", None, None)
    first = _schedule(fifo, "first", is_start=True)
    second = _schedule(fifo, "second")
    third = _schedule(fifo, "third", is_start=True)
    assert await _drain(fifo) == [first, second, third]
    fifo.close()

    priorities = NativeCrawlCoordinator(
        1,
        10,
        None,
        "lifo",
        "lifo",
        "fifo",
        "fifo",
    )
    normal = _schedule(priorities, "normal", priority="10")
    high_start = _schedule(priorities, "high-start", priority="11", is_start=True)
    equal_start = _schedule(priorities, "equal-start", priority="10", is_start=True)
    assert await _drain(priorities) == [high_start, normal, equal_start]
    priorities.close()


async def _verify_memory_precedes_disk_and_recovery() -> None:
    with tempfile.TemporaryDirectory(prefix="spideroxide-queue-state-") as temporary:
        coordinator = NativeCrawlCoordinator(
            1,
            10,
            temporary,
            "lifo",
            "lifo",
            "fifo",
            "fifo",
        )
        disk_high = _schedule(
            coordinator,
            "disk-high",
            priority="100",
            payload=b"disk-high",
        )
        memory_low = _schedule(coordinator, "memory-low", priority="-100")
        coordinator.close_input()
        assert await coordinator.next_request() == memory_low
        coordinator.complete(memory_low)
        assert await coordinator.next_request() == disk_high
        coordinator.release(disk_high)
        coordinator.abort()
        coordinator.close()

        resumed = NativeCrawlCoordinator(
            1,
            10,
            temporary,
            "lifo",
            "lifo",
            "fifo",
            "fifo",
        )
        assert resumed.take_recovered() == [(disk_high, b"disk-high")]
        assert await _drain(resumed) == [disk_high]
        resumed.close()

    with tempfile.TemporaryDirectory(prefix="spideroxide-start-state-") as temporary:
        coordinator = NativeCrawlCoordinator(
            1,
            10,
            temporary,
            "lifo",
            "lifo",
            "fifo",
            "fifo",
        )
        normal_one = _schedule(coordinator, "normal-1", payload=b"normal-1")
        start_one = _schedule(
            coordinator,
            "start-1",
            payload=b"start-1",
            is_start=True,
        )
        normal_two = _schedule(coordinator, "normal-2", payload=b"normal-2")
        start_two = _schedule(
            coordinator,
            "start-2",
            payload=b"start-2",
            is_start=True,
        )
        coordinator.abort()
        coordinator.close()

        resumed = NativeCrawlCoordinator(
            1,
            10,
            temporary,
            "lifo",
            "lifo",
            "fifo",
            "fifo",
        )
        recovered_ids = [request_id for request_id, _ in resumed.take_recovered()]
        assert recovered_ids == [normal_one, start_one, normal_two, start_two]
        assert await _drain(resumed) == [
            normal_two,
            normal_one,
            start_one,
            start_two,
        ]
        resumed.close()


async def _verify_engine_parity() -> None:
    for backend in ("python", "rust"):
        downloader = RecordingDownloader()
        result = await Crawler(
            QueueSpider,
            {
                "CONCURRENT_REQUESTS": 1,
                "ENGINE_BACKEND": backend,
            },
            downloader=downloader,
        ).crawl()
        assert downloader.urls == [
            "https://example.test/start-1",
            "https://example.test/normal-2",
            "https://example.test/normal-1",
            "https://example.test/start-2",
        ]
        assert len(result.items) == 3

        fifo_downloader = RecordingDownloader()
        await Crawler(
            QueueSpider,
            {
                "CONCURRENT_REQUESTS": 1,
                "ENGINE_BACKEND": backend,
                "SCHEDULER_MEMORY_QUEUE": "scrapy.squeues.FifoMemoryQueue",
            },
            downloader=fifo_downloader,
        ).crawl()
        assert fifo_downloader.urls == [
            "https://example.test/start-1",
            "https://example.test/normal-1",
            "https://example.test/normal-2",
            "https://example.test/start-2",
        ]

        shared_downloader = RecordingDownloader()
        await Crawler(
            QueueSpider,
            {
                "CONCURRENT_REQUESTS": 1,
                "ENGINE_BACKEND": backend,
                "SCHEDULER_MEMORY_QUEUE": "scrapy.squeues.FifoMemoryQueue",
                "SCHEDULER_START_MEMORY_QUEUE": None,
                "SCHEDULER_START_DISK_QUEUE": None,
            },
            downloader=shared_downloader,
        ).crawl()
        assert shared_downloader.urls == [
            "https://example.test/start-1",
            "https://example.test/start-2",
            "https://example.test/normal-1",
            "https://example.test/normal-2",
        ]


async def _verify_invalid_settings() -> None:
    crawler = Crawler(
        QueueSpider,
        {
            "ENGINE_BACKEND": "rust",
            "SCHEDULER_MEMORY_QUEUE": "example.UnsupportedQueue",
        },
        downloader=RecordingDownloader(),
    )
    try:
        await crawler.crawl()
    except ValueError as error:
        assert "unsupported SCHEDULER_MEMORY_QUEUE" in str(error)
    else:
        raise AssertionError("unsupported scheduler queue was accepted")


async def _verify() -> None:
    await _verify_native_queue_modes()
    await _verify_memory_precedes_disk_and_recovery()
    await _verify_engine_parity()
    await _verify_invalid_settings()


if __name__ == "__main__":
    asyncio.run(_verify())
    print(
        "Scheduler queues passed: FIFO, LIFO, start precedence, storage precedence, "
        "recovery, settings validation, and Python/Rust engine parity"
    )
