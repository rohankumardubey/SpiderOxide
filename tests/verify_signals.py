from __future__ import annotations

import asyncio
import gc
import sys
import weakref
from collections import Counter
from contextlib import suppress
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from spideroxide import (
    Crawler,
    DontCloseSpider,
    DropItem,
    Request,
    Response,
    Spider,
    StopDownload,
    signals,
)
from spideroxide.exceptions import IgnoreRequest
from spideroxide.signals import SignalManager

BODY = b"0123456789" * 4096


async def _serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        head = await reader.readuntil(b"\r\n\r\n")
        target = head.split(b" ", 2)[1]
        if target == b"/missing":
            writer.write(
                b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
            )
        else:
            writer.write(
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: application/octet-stream\r\n"
                + f"Content-Length: {len(BODY)}\r\n".encode()
                + b"Connection: close\r\n\r\n"
            )
            await writer.drain()
            for start in range(0, len(BODY), 4096):
                writer.write(BODY[start : start + 4096])
                await writer.drain()
                await asyncio.sleep(0)
    except (ConnectionError, asyncio.IncompleteReadError):
        pass
    finally:
        writer.close()
        with suppress(ConnectionError):
            await writer.wait_closed()


class SignalPipeline:
    def process_item(self, item: dict[str, object], spider: Spider) -> dict[str, object]:
        del spider
        if item["kind"] == "drop":
            raise DropItem("drop")
        if item["kind"] == "error":
            raise ValueError("pipeline failure")
        return item


class LifecycleSpider(Spider):
    name = "signal-lifecycle"

    def __init__(self, base_url: str) -> None:
        super().__init__()
        self.base_url = base_url

    async def start(self):
        yield Request(f"{self.base_url}/ignored")
        yield Request(f"{self.base_url}/ok", callback=self.parse_items)
        yield Request(f"{self.base_url}/ok", callback=self.parse_items)

    def parse_items(self, response: Response) -> list[dict[str, object]]:
        return [
            {"kind": "ok", "response": response},
            {"kind": "drop"},
            {"kind": "error"},
        ]


async def _verify_lifecycle(base_url: str, engine: str, downloader: str) -> None:
    crawler = Crawler(
        LifecycleSpider,
        {
            "ENGINE_BACKEND": engine,
            "DOWNLOADER_BACKEND": downloader,
            "CONCURRENT_REQUESTS": 2,
            "ITEM_PIPELINES": {SignalPipeline: 100},
            "RETRY_ENABLED": False,
        },
    )
    events: Counter[str] = Counter()
    item_responses: dict[str, Response | None] = {}

    def scheduled(request: Request) -> None:
        events["request_scheduled"] += 1
        if request.url.endswith("/ignored"):
            raise IgnoreRequest("ignored by signal")

    def returned_schedule_control() -> IgnoreRequest:
        return IgnoreRequest("returned controls must be ignored")

    def dropped() -> None:
        events["request_dropped"] += 1

    def scheduler_empty() -> None:
        events["scheduler_empty"] += 1

    def reached() -> None:
        events["request_reached_downloader"] += 1

    def left() -> None:
        events["request_left_downloader"] += 1

    def downloaded() -> None:
        events["response_downloaded"] += 1

    def received() -> None:
        events["response_received"] += 1

    def idle() -> None:
        events["spider_idle"] += 1

    def item_scraped(response: Response | None) -> None:
        events["item_scraped"] += 1
        item_responses["scraped"] = response

    def item_dropped(response: Response | None) -> None:
        events["item_dropped"] += 1
        item_responses["dropped"] = response

    def item_error(response: Response | None, failure: Exception) -> None:
        events["item_error"] += 1
        item_responses["error"] = response
        assert str(failure) == "pipeline failure"

    for signal, receiver in (
        (signals.request_scheduled, scheduled),
        (signals.request_scheduled, returned_schedule_control),
        (signals.request_dropped, dropped),
        (signals.scheduler_empty, scheduler_empty),
        (signals.request_reached_downloader, reached),
        (signals.request_left_downloader, left),
        (signals.response_downloaded, downloaded),
        (signals.response_received, received),
        (signals.spider_idle, idle),
        (signals.item_scraped, item_scraped),
        (signals.item_dropped, item_dropped),
        (signals.item_error, item_error),
    ):
        crawler.signals.connect(receiver, signal)

    result = await crawler.crawl(base_url)
    assert result.reason == "finished"
    assert [item["kind"] for item in result.items] == ["ok"]
    assert events["request_scheduled"] == 3
    assert events["request_dropped"] == 1
    assert events["scheduler_empty"] >= 1
    assert events["request_reached_downloader"] == 1
    assert events["request_left_downloader"] == 1
    assert events["response_downloaded"] == 1
    assert events["response_received"] == 1
    assert events["spider_idle"] == 1
    assert events["item_scraped"] == 1
    assert events["item_dropped"] == 1
    assert events["item_error"] == 1
    response = item_responses["scraped"]
    assert response is not None and response.url.endswith("/ok")
    assert item_responses == {
        "scraped": response,
        "dropped": response,
        "error": response,
    }
    assert result.stats["scheduler/ignored"] == 1


class StopSpider(Spider):
    name = "signal-stop-download"

    def __init__(self, base_url: str) -> None:
        super().__init__()
        self.base_url = base_url

    async def start(self):
        yield Request(f"{self.base_url}/headers-stop", callback=self.parse)
        yield Request(f"{self.base_url}/bytes-stop", callback=self.parse)
        yield Request(
            f"{self.base_url}/bytes-fail",
            callback=self.parse,
            errback=self.failed,
        )
        yield Request(f"{self.base_url}/returned-stop", callback=self.parse)

    def parse(self, response: Response) -> dict[str, object]:
        return {
            "path": response.url.rsplit("/", 1)[-1],
            "length": len(response.body),
            "flags": response.flags,
        }

    def failed(self, exception: Exception) -> dict[str, object]:
        assert isinstance(exception, StopDownload)
        assert isinstance(exception.response, Response)
        return {
            "path": "bytes-fail",
            "length": len(exception.response.body),
            "flags": exception.response.flags,
        }


async def _verify_streaming(base_url: str, engine: str, downloader: str) -> None:
    crawler = Crawler(
        StopSpider,
        {
            "ENGINE_BACKEND": engine,
            "DOWNLOADER_BACKEND": downloader,
            "CONCURRENT_REQUESTS": 1,
            "RETRY_ENABLED": False,
        },
    )
    events: Counter[str] = Counter()
    received = bytearray()
    loop = asyncio.get_running_loop()

    def headers_received(request: Request, body_length: int | None) -> StopDownload | None:
        assert asyncio.get_running_loop() is loop
        events[f"headers:{request.url.rsplit('/', 1)[-1]}"] += 1
        assert body_length == len(BODY)
        if request.url.endswith("/headers-stop"):
            raise StopDownload(fail=False)
        if request.url.endswith("/returned-stop"):
            return StopDownload(fail=False)
        return None

    def bytes_received(data: bytes, request: Request) -> None:
        assert asyncio.get_running_loop() is loop
        name = request.url.rsplit("/", 1)[-1]
        events[f"bytes:{name}"] += 1
        received.extend(data)
        if name == "bytes-stop":
            raise StopDownload(fail=False)
        if name == "bytes-fail":
            raise StopDownload()

    crawler.signals.connect(headers_received, signals.headers_received)
    crawler.signals.connect(bytes_received, signals.bytes_received)
    result = await crawler.crawl(base_url)
    items = {item["path"]: item for item in result.items}
    assert items["headers-stop"]["length"] == 0
    assert 0 < items["bytes-stop"]["length"] <= len(BODY)
    assert 0 < items["bytes-fail"]["length"] <= len(BODY)
    assert items["headers-stop"]["flags"] == ("download_stopped",)
    assert items["bytes-stop"]["flags"] == ("download_stopped",)
    assert items["bytes-fail"]["flags"] == ("download_stopped",)
    assert items["returned-stop"]["length"] == len(BODY)
    assert items["returned-stop"]["flags"] == ()
    assert events["headers:headers-stop"] == 1
    assert events["headers:bytes-stop"] == 1
    assert events["headers:bytes-fail"] == 1
    assert events["headers:returned-stop"] == 1
    assert events["bytes:headers-stop"] == 0
    assert events["bytes:bytes-stop"] >= 1
    assert events["bytes:bytes-fail"] >= 1
    assert received


class SingleRequestSpider(Spider):
    name = "signal-single-request"

    def __init__(self, url: str) -> None:
        super().__init__()
        self.url = url

    def start_requests(self):
        yield Request(self.url)

    def parse(self, response: Response) -> dict[str, object]:
        return {"length": len(response.body), "flags": response.flags, "url": response.url}


async def _verify_stop_precedes_size_limit(base_url: str, downloader: str) -> None:
    crawler = Crawler(
        SingleRequestSpider,
        {
            "ENGINE_BACKEND": "python",
            "DOWNLOADER_BACKEND": downloader,
            "DOWNLOAD_MAXSIZE": 5,
            "RETRY_ENABLED": False,
        },
    )

    def stop_at_headers() -> None:
        raise StopDownload(fail=False)

    crawler.signals.connect(stop_at_headers, signals.headers_received)
    result = await crawler.crawl(f"{base_url}/oversized")
    assert result.items == (
        {
            "length": 0,
            "flags": ("download_stopped",),
            "url": f"{base_url}/oversized",
        },
    )


async def _verify_idle_resume(base_url: str, engine: str) -> None:
    crawler = Crawler(
        SingleRequestSpider,
        {
            "ENGINE_BACKEND": engine,
            "RETRY_ENABLED": False,
        },
    )
    idle_count = 0

    async def keep_open() -> None:
        nonlocal idle_count
        idle_count += 1
        if idle_count != 1:
            return
        assert crawler.engine is not None
        await crawler.engine._schedule(Request(f"{base_url}/idle"))
        raise DontCloseSpider

    crawler.signals.connect(keep_open, signals.spider_idle)
    result = await crawler.crawl(f"{base_url}/initial")
    assert {item["url"] for item in result.items} == {
        f"{base_url}/initial",
        f"{base_url}/idle",
    }
    assert idle_count == 2


class LazyStartSpider(Spider):
    name = "signal-lazy-start"

    def __init__(self, base_url: str, scheduler_empty: asyncio.Event) -> None:
        super().__init__()
        self.base_url = base_url
        self.scheduler_empty = scheduler_empty

    async def start(self):
        yield Request(f"{self.base_url}/first")
        await self.scheduler_empty.wait()
        yield Request(f"{self.base_url}/second")

    def parse(self, response: Response) -> dict[str, str]:
        return {"url": response.url}


async def _verify_lazy_start(base_url: str, engine: str) -> None:
    scheduler_empty = asyncio.Event()
    crawler = Crawler(
        LazyStartSpider,
        {
            "ENGINE_BACKEND": engine,
            "CONCURRENT_REQUESTS": 1,
            "RETRY_ENABLED": False,
        },
    )
    crawler.signals.connect(scheduler_empty.set, signals.scheduler_empty)
    result = await asyncio.wait_for(crawler.crawl(base_url, scheduler_empty), 5)
    assert {item["url"] for item in result.items} == {
        f"{base_url}/first",
        f"{base_url}/second",
    }


async def _verify_signal_helpers() -> None:
    manager = SignalManager()
    seen: list[str] = []

    def good() -> str:
        seen.append("good")
        return "ok"

    async def bad() -> None:
        raise ValueError("signal failure")

    manager.connect(good, "ready")
    manager.connect(bad, "ready")
    responses = await manager.send_catch_log_async("ready", dont_log=ValueError)
    assert responses[0] == (good, "ok")
    assert responses[1][0] is bad
    assert isinstance(responses[1][1], ValueError)
    assert seen == ["good"]

    waiter = asyncio.create_task(manager.wait_for("ready"))
    await asyncio.sleep(0)
    assert not waiter.done()
    await manager.send("ready", dont_log=ValueError)
    await asyncio.wait_for(waiter, 1)
    assert manager._receivers["ready"] == [good, bad]

    cancelled = asyncio.create_task(manager.wait_for("never"))
    await asyncio.sleep(0)
    cancelled.cancel()
    with suppress(asyncio.CancelledError):
        await cancelled
    assert not manager._receivers["never"]

    manager.disconnect_all("ready")
    assert manager.send_sync("ready") == []
    assert not manager.disconnect(good, "ready")

    scope = object()
    other_scope = object()
    payload: list[tuple[object, object, int]] = []

    def scoped(signal: object, sender: object, value: int) -> int:
        payload.append((signal, sender, value))
        return value + 1

    manager.connect(scoped, scope, sender=scope)
    manager.connect(scoped, scope, sender=scope)
    assert manager.send_sync(scope, sender=other_scope, value=3) == []
    assert manager.send_sync(scope, sender=scope, value=3) == [(scoped, 4)]
    assert payload == [(scope, scope, 3)]
    assert manager.disconnect(scoped, scope, sender=other_scope) is False
    manager.disconnect_all(scope, sender=scope)
    assert manager.send_sync(scope, sender=scope, value=3) == []

    marker = object()
    manager.connect(scoped, marker)
    assert manager.send_sync(marker, value=4) == [(scoped, 5)]
    assert manager.send_catch_log(marker, value=4) == [(scoped, 5)]
    assert payload[-1] == (marker, manager.sender, 4)
    assert manager.disconnect(scoped, marker)

    class Receiver:
        def handle(self) -> str:
            return "alive"

    weak_receiver = Receiver()
    receiver_ref = weakref.ref(weak_receiver)
    manager.connect(weak_receiver.handle, "weak", weak=True)
    assert manager.send_sync("weak")[0][1] == "alive"
    assert manager.disconnect(weak_receiver.handle, "weak")
    manager.connect(weak_receiver.handle, "weak", weak=True)
    del weak_receiver
    gc.collect()
    assert receiver_ref() is None
    assert manager.send_sync("weak") == []

    def later() -> str:
        return "later"

    def mutate() -> str:
        manager.disconnect(later, "snapshot")
        manager.connect(lambda: "new", "snapshot")
        return "first"

    manager.connect(mutate, "snapshot")
    manager.connect(later, "snapshot")
    assert manager.send_sync("snapshot") == [(mutate, "first"), (later, "later")]

    entered = asyncio.Event()
    release = asyncio.Event()

    async def blocked() -> str:
        entered.set()
        await release.wait()
        return "finished"

    async def unblock() -> str:
        await entered.wait()
        release.set()
        return "released"

    manager.connect(blocked, "concurrent")
    manager.connect(unblock, "concurrent")
    assert await asyncio.wait_for(manager.send_catch_log_async("concurrent"), 1) == [
        (blocked, "finished"),
        (unblock, "released"),
    ]

    def stopped() -> None:
        raise StopDownload(fail=False)

    manager.connect(stopped, "stopped")
    with patch("spideroxide.signals.logger.exception") as logged:
        responses = manager.send_sync("stopped")
        async_responses = await manager.send_catch_log_async("stopped")
    assert isinstance(responses[0][1], signals.SignalFailure)
    assert isinstance(async_responses[0][1], StopDownload)
    logged.assert_not_called()
    from twisted.python.failure import Failure

    with patch("spideroxide.signals.logger.exception") as logged:
        scrapy_responses = manager.send_catch_log("stopped")
    assert scrapy_responses[0][0] is stopped
    assert isinstance(scrapy_responses[0][1], Failure)
    assert isinstance(scrapy_responses[0][1].value, StopDownload)
    logged.assert_not_called()

    def broken(value: int) -> None:
        raise ValueError(f"failure {value}")

    manager.connect(broken, "scrapy-sync")
    manager.connect(scoped, "scrapy-sync")
    with patch("spideroxide.signals.logger.exception") as logged:
        scrapy_responses = manager.send_catch_log("scrapy-sync", value=7)
    assert scrapy_responses[0][0] is broken
    assert isinstance(scrapy_responses[0][1], Failure)
    assert scrapy_responses[0][1].type is ValueError
    assert str(scrapy_responses[0][1].value) == "failure 7"
    assert scrapy_responses[1] == (scoped, 8)
    logged.assert_called_once()

    returned = signals.SignalFailure(ValueError("returned, not raised"))
    manager.connect(lambda: returned, "returned")
    assert manager.send_catch_log("returned")[0][1] is returned

    with patch.dict(sys.modules, {"twisted.python.failure": None}):
        try:
            manager.send_catch_log("scrapy-sync", dont_log=ValueError, value=7)
        except RuntimeError as error:
            assert "spideroxide[twisted]" in str(error)
        else:
            raise AssertionError("Scrapy-style failures without Twisted should fail explicitly")

    from twisted.internet.defer import Deferred

    pending: Deferred[object] = Deferred()

    def deferred_receiver() -> Deferred[object]:
        return pending

    manager.connect(deferred_receiver, "deferred")
    with patch("spideroxide.signals.logger.error") as logged:
        deferred_responses = manager.send_catch_log("deferred")
    assert deferred_responses == [(deferred_receiver, pending)]
    logged.assert_called_once()
    assert "Cannot return deferreds" in logged.call_args.args[0]
    with patch("spideroxide.signals.logger.exception") as logged:
        native_responses = manager.send_sync("deferred")
    assert isinstance(native_responses[0][1], signals.SignalFailure)
    assert isinstance(native_responses[0][1].exception, TypeError)
    logged.assert_called_once()

    async def unexpected_async() -> None:
        return None

    manager.connect(unexpected_async, "sync-only")
    coroutine_responses = manager.send_catch_log("sync-only")
    assert coroutine_responses[0][0] is unexpected_async
    assert asyncio.iscoroutine(coroutine_responses[0][1])
    coroutine_responses[0][1].close()
    with patch("spideroxide.signals.logger.exception"):
        sync_responses = manager.send_sync("sync-only")
    assert isinstance(sync_responses[0][1], signals.SignalFailure)
    assert isinstance(sync_responses[0][1].exception, TypeError)


async def _verify() -> None:
    await _verify_signal_helpers()
    server = await asyncio.start_server(_serve, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    base_url = f"http://127.0.0.1:{port}"
    try:
        for engine in ("python", "rust"):
            for downloader in ("python", "rust"):
                await _verify_lifecycle(base_url, engine, downloader)
                await _verify_streaming(base_url, engine, downloader)
        for downloader in ("python", "rust"):
            await _verify_stop_precedes_size_limit(base_url, downloader)
        for engine in ("python", "rust"):
            await _verify_idle_resume(base_url, engine)
            await _verify_lazy_start(base_url, engine)
    finally:
        server.close()
        await server.wait_closed()


if __name__ == "__main__":
    asyncio.run(_verify())
    print(
        "Signals passed: scheduling controls, scheduler and downloader lifecycle, streaming, "
        "partial downloads, item failures, async helpers, and Python/Rust parity"
    )
