from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from spideroxide._native import NativeCrawlCoordinator

from spideroxide import (
    CloseSpider,
    Crawler,
    Headers,
    NativeCrawlEngine,
    Request,
    Response,
    Spider,
)


class BlockingDownloader:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.closed = False

    async def fetch(self, request: Request) -> Response:
        self.started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    async def close(self) -> None:
        self.closed = True


class RecordingDownloader:
    def __init__(self) -> None:
        self.requests: list[Request] = []
        self.closed = False

    async def fetch(self, request: Request) -> Response:
        self.requests.append(request)
        return Response(request.url, request=request)

    async def close(self) -> None:
        self.closed = True


class ResumableSpider(Spider):
    name = "resumable"

    async def start(self):
        if self.state.get("seeded"):
            return
        self.state["seeded"] = True
        for index in range(6):
            headers = Headers()
            headers.setlist("X-Value", [f"first-{index}", f"second-{index}"])
            yield Request(
                f"https://example.test/{index}",
                callback=self.parse_page,
                headers=headers,
                cookies={"session": str(index)},
                meta={"index": index, "nested": [index]},
                priority=index,
                flags=("persistent",),
                cb_kwargs={"expected": index},
            )

    def parse_page(self, response: Response, expected: int) -> dict[str, int]:
        assert response.request is not None
        request = response.request
        assert request.meta["index"] == expected
        assert request.meta["nested"] == [expected]
        assert request.headers.getlist("X-Value") == [
            f"first-{expected}".encode(),
            f"second-{expected}".encode(),
        ]
        assert request.cookies == {"session": str(expected)}
        assert request.flags == ("persistent",)
        self.state["processed"] = self.state.get("processed", 0) + 1
        return {"index": expected}


class UnserializableSpider(Spider):
    name = "unserializable"

    async def start(self):
        if self.state.get("seeded"):
            return
        self.state["seeded"] = True

        def local_callback(response: Response) -> dict[str, bool]:
            return {"unexpected": True}

        yield Request("https://example.test/transient", callback=local_callback)


class CrashSpider(Spider):
    name = "crash"

    async def start(self):
        for index in range(4):
            yield Request(
                f"https://example.test/crash/{index}",
                callback=self.parse_page,
                priority=index,
            )
        if os.environ.get("SPIDEROXIDE_CRASH_WORKER") == "1":
            os._exit(17)

    def parse_page(self, response: Response) -> dict[str, str]:
        return {"url": response.url}


class CloseOnceSpider(Spider):
    name = "close-once"

    async def start(self):
        if self.state.get("seeded"):
            return
        self.state["seeded"] = True
        yield Request("https://example.test/close", callback=self.parse_page)

    def parse_page(self, response: Response) -> dict[str, bool]:
        if not self.state.get("stopped"):
            self.state["stopped"] = True
            raise CloseSpider("paused")
        return {"resumed": True}


async def _wait_for_stat(crawler: Crawler, name: str, value: int) -> None:
    async def wait() -> None:
        while crawler.stats.get_value(name, 0) != value:
            await asyncio.sleep(0)

    await asyncio.wait_for(wait(), timeout=2)


async def _cancel_crawl(crawler: Crawler, downloader: BlockingDownloader, count: int) -> None:
    crawl = asyncio.create_task(crawler.crawl())
    await asyncio.wait_for(downloader.started.wait(), timeout=2)
    await _wait_for_stat(crawler, "scheduler/enqueued", count)
    crawl.cancel()
    try:
        await crawl
    except asyncio.CancelledError:
        pass
    else:
        raise AssertionError("persistent crawl cancellation did not propagate")
    assert downloader.closed is True
    assert crawler.stats.get_value("finish_reason") == "cancelled"


async def _verify_native_store(directory: Path) -> None:
    coordinator = NativeCrawlCoordinator(1, 10, str(directory))
    low = coordinator.schedule(
        "https://example.test/low",
        "GET",
        b"",
        "1",
        True,
        b"low",
    )
    huge_priority = "1" + ("0" * 100)
    high_one = coordinator.schedule(
        "https://example.test/high-one",
        "GET",
        b"",
        huge_priority,
        True,
        b"high-one",
    )
    high_two = coordinator.schedule(
        "https://example.test/high-two",
        "GET",
        b"",
        huge_priority,
        True,
        b"high-two",
    )
    assert (low, high_one, high_two) == (0, 1, 2)
    for request_id in (low, high_one, high_two):
        coordinator.activate(request_id)

    try:
        NativeCrawlCoordinator(1, 10, str(directory))
    except RuntimeError as error:
        assert "already in use" in str(error)
    else:
        raise AssertionError("JOBDIR accepted two concurrent owners")

    assert await coordinator.next_request() == high_one
    coordinator.release(high_one)
    coordinator.abort()
    coordinator.close()

    resumed = NativeCrawlCoordinator(1, 10, str(directory))
    assert resumed.persistent is True
    assert resumed.recovered_count == 3
    assert resumed.take_recovered() == [
        (low, b"low"),
        (high_one, b"high-one"),
        (high_two, b"high-two"),
    ]
    assert (
        resumed.schedule(
            "https://example.test/low",
            "GET",
            b"",
            "999",
            True,
            b"duplicate",
        )
        is None
    )
    resumed.close_input()
    order = []
    while (request_id := await resumed.next_request()) is not None:
        order.append(request_id)
        resumed.complete(request_id)
    assert order == [high_one, high_two, low]
    resumed.close()

    finished = NativeCrawlCoordinator(1, 10, str(directory))
    assert finished.recovered_count == 0
    assert finished.seen_count == 3
    finished.close()


async def _verify_crawl_resume(directory: Path) -> None:
    blocking = BlockingDownloader()
    crawler = Crawler(
        ResumableSpider,
        {
            "CONCURRENT_REQUESTS": 1,
            "ENGINE_BACKEND": "rust",
            "ENGINE_MAX_PENDING": 10,
            "JOBDIR": directory,
        },
        downloader=blocking,
    )
    await _cancel_crawl(crawler, blocking, 6)
    assert crawler.stats.get_value("scheduler/enqueued/disk") == 6
    assert crawler.stats.get_value("scheduler/dequeued/disk") == 1
    assert (directory / "job.sqlite3").is_file()
    assert (directory / ".spideroxide.lock").is_file()

    recording = RecordingDownloader()
    resumed = Crawler(
        ResumableSpider,
        {
            "CONCURRENT_REQUESTS": 1,
            "ENGINE_BACKEND": "rust",
            "ENGINE_MAX_PENDING": 10,
            "JOBDIR": directory,
        },
        downloader=recording,
    )
    result = await resumed.crawl()
    assert isinstance(resumed.engine, NativeCrawlEngine)
    assert recording.closed is True
    assert [request.url for request in recording.requests] == [
        f"https://example.test/{index}" for index in reversed(range(6))
    ]
    assert result.items == tuple({"index": index} for index in reversed(range(6)))
    assert result.stats["scheduler/recovered"] == 6
    assert result.stats["scheduler/dequeued/disk"] == 6
    assert resumed.spider is not None
    assert resumed.spider.state == {"seeded": True, "processed": 6}

    final = Crawler(
        ResumableSpider,
        {
            "ENGINE_BACKEND": "auto",
            "JOBDIR": directory,
        },
        downloader=RecordingDownloader(),
    )
    final_result = await final.crawl()
    assert isinstance(final.engine, NativeCrawlEngine)
    assert final_result.items == ()
    assert final.spider is not None
    assert final.spider.state == {"seeded": True, "processed": 6}


async def _verify_unserializable_fallback(directory: Path) -> None:
    blocking = BlockingDownloader()
    crawler = Crawler(
        UnserializableSpider,
        {
            "CONCURRENT_REQUESTS": 1,
            "ENGINE_BACKEND": "rust",
            "JOBDIR": directory,
            "SCHEDULER_DEBUG": True,
        },
        downloader=blocking,
    )
    previous_disable = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    try:
        await _cancel_crawl(crawler, blocking, 1)
    finally:
        logging.disable(previous_disable)
    assert crawler.stats.get_value("scheduler/unserializable") == 1
    assert crawler.stats.get_value("scheduler/enqueued/memory") == 1

    recording = RecordingDownloader()
    resumed = Crawler(
        UnserializableSpider,
        {
            "ENGINE_BACKEND": "rust",
            "JOBDIR": directory,
        },
        downloader=recording,
    )
    result = await resumed.crawl()
    assert recording.requests == []
    assert result.stats.get("scheduler/recovered") is None


async def _run_crash_worker(directory: Path) -> None:
    await Crawler(
        CrashSpider,
        {
            "CONCURRENT_REQUESTS": 1,
            "ENGINE_BACKEND": "rust",
            "ENGINE_MAX_PENDING": 10,
            "JOBDIR": directory,
        },
        downloader=BlockingDownloader(),
    ).crawl()


async def _verify_hard_crash(directory: Path) -> None:
    environment = dict(os.environ)
    environment["SPIDEROXIDE_CRASH_WORKER"] = "1"
    process = subprocess.run(
        [sys.executable, __file__, "--crash-worker", str(directory)],
        check=False,
        env=environment,
        timeout=10,
    )
    assert process.returncode == 17

    downloader = RecordingDownloader()
    crawler = Crawler(
        CrashSpider,
        {
            "CONCURRENT_REQUESTS": 1,
            "ENGINE_BACKEND": "rust",
            "ENGINE_MAX_PENDING": 10,
            "JOBDIR": directory,
        },
        downloader=downloader,
    )
    result = await crawler.crawl()
    assert [request.url for request in downloader.requests] == [
        f"https://example.test/crash/{index}" for index in reversed(range(4))
    ]
    assert result.stats["scheduler/recovered"] == 4
    assert result.stats["dupefilter/filtered"] == 4
    assert result.items == tuple(
        {"url": f"https://example.test/crash/{index}"} for index in reversed(range(4))
    )


async def _verify_graceful_close_resume(directory: Path) -> None:
    first = await Crawler(
        CloseOnceSpider,
        {
            "ENGINE_BACKEND": "rust",
            "JOBDIR": directory,
        },
        downloader=RecordingDownloader(),
    ).crawl()
    assert first.reason == "paused"

    crawler = Crawler(
        CloseOnceSpider,
        {
            "ENGINE_BACKEND": "rust",
            "JOBDIR": directory,
        },
        downloader=RecordingDownloader(),
    )
    resumed = await crawler.crawl()
    assert resumed.reason == "finished"
    assert resumed.items == ({"resumed": True},)
    assert resumed.stats["scheduler/recovered"] == 1


async def _verify_python_rejection(directory: Path) -> None:
    downloader = RecordingDownloader()
    crawler = Crawler(
        ResumableSpider,
        {
            "ENGINE_BACKEND": "python",
            "JOBDIR": directory,
        },
        downloader=downloader,
    )
    try:
        await crawler.crawl()
    except ValueError as error:
        assert "requires ENGINE_BACKEND" in str(error)
    else:
        raise AssertionError("Python engine accepted native JOBDIR persistence")
    assert downloader.closed is True


def _verify_schema_rejection(directory: Path) -> None:
    coordinator = NativeCrawlCoordinator(1, 1, str(directory))
    coordinator.close()
    with sqlite3.connect(directory / "job.sqlite3") as connection:
        connection.execute("UPDATE metadata SET value = 999 WHERE key = 'schema_version'")
    try:
        NativeCrawlCoordinator(1, 1, str(directory))
    except RuntimeError as error:
        assert "schema version 999" in str(error)
    else:
        raise AssertionError("native store accepted an incompatible schema")


async def _verify() -> None:
    with tempfile.TemporaryDirectory(prefix="spideroxide-native-store-") as temporary:
        await _verify_native_store(Path(temporary))
    with tempfile.TemporaryDirectory(prefix="spideroxide-crawl-resume-") as temporary:
        await _verify_crawl_resume(Path(temporary))
    with tempfile.TemporaryDirectory(prefix="spideroxide-unserializable-") as temporary:
        await _verify_unserializable_fallback(Path(temporary))
    with tempfile.TemporaryDirectory(prefix="spideroxide-hard-crash-") as temporary:
        await _verify_hard_crash(Path(temporary))
    with tempfile.TemporaryDirectory(prefix="spideroxide-close-resume-") as temporary:
        await _verify_graceful_close_resume(Path(temporary))
    with tempfile.TemporaryDirectory(prefix="spideroxide-python-jobdir-") as temporary:
        await _verify_python_rejection(Path(temporary))
    with tempfile.TemporaryDirectory(prefix="spideroxide-schema-") as temporary:
        _verify_schema_rejection(Path(temporary))


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--crash-worker":
        asyncio.run(_run_crash_worker(Path(sys.argv[2])))
    else:
        asyncio.run(_verify())
        print(
            "Persistent job state passed: Rust WAL storage, locking, recovery, priority, "
            "fingerprints, callbacks, request data, spider state, cancellation, graceful stops, "
            "hard crashes, memory fallback, schema checks, and auto-engine selection"
        )
