from __future__ import annotations

import asyncio
import json
import logging
import marshal
import os
import pickle
import sqlite3
import struct
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
    FormRequest,
    Headers,
    JsonRequest,
    NativeCrawlEngine,
    Request,
    Response,
    Spider,
)
from spideroxide.job import deserialize_request, serialize_request
from spideroxide.jobinterop import migrate_scrapy_jobdir
from spideroxide.settings import Settings


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


class UnfilteredResumeSpider(Spider):
    name = "unfiltered-resume"

    async def start(self):
        if self.state.get("seeded"):
            return
        self.state["seeded"] = True
        yield Request("https://example.test/unfiltered", dont_filter=True)


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


class ScrapyImportedSpider(Spider):
    name = "scrapy-imported"

    async def start(self):
        if not self.state.get("seeded"):
            raise AssertionError("Scrapy spider state was not imported")
        if False:
            yield

    def parse_page(self, response: Response, label: str) -> dict[str, str]:
        assert response.request is not None
        assert response.request.headers.getlist("X-Source") == [b"scrapy", label.encode()]
        self.state["processed"] = self.state.get("processed", 0) + 1
        return {"label": label}


def _scrapy_request(
    label: str,
    priority: int,
    *,
    start: bool = False,
    request_class: str | None = None,
) -> dict[str, object]:
    values: dict[str, object] = {
        "url": f"https://example.test/scrapy/{label}",
        "callback": "parse_page",
        "errback": None,
        "method": "POST",
        "headers": {b"X-Source": [b"scrapy", label.encode()]},
        "body": label.encode(),
        "cookies": {"session": label},
        "meta": {"is_start_request": start, "source": "scrapy"},
        "encoding": "utf-8",
        "priority": priority,
        "dont_filter": False,
        "flags": ["persisted"],
        "cb_kwargs": {"label": label},
    }
    if request_class is not None:
        values["_class"] = request_class
    if request_class == "scrapy.http.request.json_request.JsonRequest":
        values["dumps_kwargs"] = {"sort_keys": False}
    return values


def _serialize_scrapy_queue_item(values: object, serialization: str) -> bytes:
    if serialization == "pickle":
        return pickle.dumps(values, protocol=4)
    return marshal.dumps(values)


def _write_lifo_queue(path: Path, items: list[bytes]) -> None:
    payload = bytearray(struct.pack(">L", len(items)))
    for item in items:
        payload.extend(item)
        payload.extend(struct.pack(">L", len(item)))
    path.write_bytes(payload)


def _write_fifo_queue(path: Path, items: list[bytes]) -> None:
    path.mkdir()
    chunk = bytearray()
    for item in items:
        chunk.extend(struct.pack(">L", len(item)))
        chunk.extend(item)
    (path / "q00000").write_bytes(chunk)
    (path / "info.json").write_text(
        json.dumps(
            {
                "chunksize": 100000,
                "size": len(items),
                "tail": [0, 0, 0],
                "head": [0, len(items)],
            }
        ),
        encoding="utf-8",
    )


def _write_scrapy_queue(
    path: Path,
    values: list[dict[str, object]],
    *,
    serialization: str,
    order: str,
) -> None:
    items = [_serialize_scrapy_queue_item(value, serialization) for value in values]
    if order == "fifo":
        _write_fifo_queue(path, items)
    else:
        _write_lifo_queue(path, items)


def _scrapy_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "SCHEDULER_DISK_QUEUE": "scrapy.squeues.PickleLifoDiskQueue",
        "SCHEDULER_START_DISK_QUEUE": "scrapy.squeues.PickleFifoDiskQueue",
    }
    values.update(overrides)
    return Settings(values)


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


def _verify_request_subclass_roundtrip() -> None:
    class CustomRequest(Request):
        pass

    spider = ResumableSpider()
    requests = [
        FormRequest(
            "https://example.test/form",
            formdata={"name": "value"},
        ),
        JsonRequest(
            "https://example.test/json",
            data={"name": "value"},
            dumps_kwargs={"sort_keys": False},
        ),
        CustomRequest("https://example.test/custom"),
    ]
    restored = [
        deserialize_request(serialize_request(request, spider), spider) for request in requests
    ]
    assert type(restored[0]) is FormRequest
    assert restored[0].body == requests[0].body
    assert type(restored[1]) is JsonRequest
    assert restored[1].body == requests[1].body
    assert restored[1].dumps_kwargs == {"sort_keys": False}
    assert type(restored[2]) is Request


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


async def _verify_unfiltered_recovery(directory: Path) -> None:
    blocking = BlockingDownloader()
    crawler = Crawler(
        UnfilteredResumeSpider,
        {
            "CONCURRENT_REQUESTS": 1,
            "ENGINE_BACKEND": "rust",
            "JOBDIR": directory,
        },
        downloader=blocking,
    )
    await _cancel_crawl(crawler, blocking, 1)

    recording = RecordingDownloader()
    resumed = Crawler(
        UnfilteredResumeSpider,
        {
            "CONCURRENT_REQUESTS": 1,
            "ENGINE_BACKEND": "rust",
            "JOBDIR": directory,
        },
        downloader=recording,
    )
    await resumed.crawl()
    assert [request.url for request in recording.requests] == ["https://example.test/unfiltered"]
    with sqlite3.connect(directory / "job.sqlite3") as connection:
        fingerprints = connection.execute("SELECT COUNT(*) FROM fingerprints").fetchone()
    assert fingerprints == (0,)


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


def _verify_schema_migration(directory: Path) -> None:
    directory.mkdir()
    with sqlite3.connect(directory / "job.sqlite3") as connection:
        connection.executescript(
            """
            CREATE TABLE metadata (
                key TEXT PRIMARY KEY,
                value INTEGER NOT NULL
            );
            CREATE TABLE requests (
                request_id INTEGER PRIMARY KEY,
                sequence INTEGER NOT NULL UNIQUE,
                priority TEXT NOT NULL,
                payload BLOB NOT NULL
            );
            INSERT INTO metadata(key, value) VALUES ('schema_version', 1);
            INSERT INTO requests(request_id, sequence, priority, payload)
            VALUES (7, 3, '11', X'6C6567616379');
            """
        )

    coordinator = NativeCrawlCoordinator(1, 1, str(directory))
    assert coordinator.take_recovered() == [(7, b"legacy")]
    coordinator.close()

    with sqlite3.connect(directory / "job.sqlite3") as connection:
        version = connection.execute(
            "SELECT value FROM metadata WHERE key = 'schema_version'"
        ).fetchone()
        columns = {row[1] for row in connection.execute("PRAGMA table_info(requests)").fetchall()}
    assert version == (3,)
    assert "is_start" in columns


def _verify_fingerprint_schema_migration(directory: Path) -> None:
    directory.mkdir()
    with sqlite3.connect(directory / "job.sqlite3") as connection:
        connection.executescript(
            """
            CREATE TABLE metadata (
                key TEXT PRIMARY KEY,
                value INTEGER NOT NULL
            );
            CREATE TABLE fingerprints (
                fingerprint BLOB PRIMARY KEY
            );
            CREATE TABLE requests (
                request_id INTEGER PRIMARY KEY,
                sequence INTEGER NOT NULL UNIQUE,
                priority TEXT NOT NULL,
                is_start INTEGER NOT NULL CHECK (is_start IN (0, 1)),
                payload BLOB NOT NULL
            );
            INSERT INTO metadata(key, value) VALUES ('schema_version', 2);
            INSERT INTO fingerprints(fingerprint) VALUES (zeroblob(32));
            """
        )

    coordinator = NativeCrawlCoordinator(1, 1, str(directory))
    coordinator.close()

    with sqlite3.connect(directory / "job.sqlite3") as connection:
        version = connection.execute(
            "SELECT value FROM metadata WHERE key = 'schema_version'"
        ).fetchone()
        fingerprints = connection.execute("SELECT COUNT(*) FROM fingerprints").fetchone()
    assert version == (3,)
    assert fingerprints == (0,)


def _create_scrapy_jobdir(directory: Path) -> tuple[bytes, bytes]:
    queue = directory / "requests.queue"
    queue.mkdir(parents=True)
    (queue / "active.json").write_text(json.dumps([-10, -1]), encoding="utf-8")
    _write_scrapy_queue(
        queue / "-10",
        [
            _scrapy_request(
                "normal-form",
                10,
                request_class="scrapy.http.request.form.FormRequest",
            ),
            _scrapy_request(
                "normal-json",
                10,
                request_class="scrapy.http.request.json_request.JsonRequest",
            ),
        ],
        serialization="pickle",
        order="lifo",
    )
    _write_scrapy_queue(
        queue / "-10s",
        [
            _scrapy_request("start-first", 10, start=True),
            _scrapy_request("start-second", 10, start=True),
        ],
        serialization="pickle",
        order="fifo",
    )
    _write_scrapy_queue(
        queue / "-1",
        [_scrapy_request("low", 1)],
        serialization="pickle",
        order="lifo",
    )
    first_fingerprint = b"a" * 20
    second_fingerprint = b"b" * 20
    (directory / "requests.seen").write_bytes(
        b"".join(
            len(fingerprint).to_bytes(2, "big") + fingerprint
            for fingerprint in (first_fingerprint, second_fingerprint)
        )
    )
    (directory / "spider.state").write_bytes(
        pickle.dumps({"seeded": True, "source": "scrapy"}, protocol=4)
    )
    return first_fingerprint, second_fingerprint


async def _verify_scrapy_jobdir_resume(directory: Path) -> None:
    fingerprints = _create_scrapy_jobdir(directory)
    source_files = {
        path.relative_to(directory): path.read_bytes()
        for path in directory.rglob("*")
        if path.is_file()
    }
    downloader = RecordingDownloader()
    crawler = Crawler(
        ScrapyImportedSpider,
        {
            "CONCURRENT_REQUESTS": 1,
            "ENGINE_BACKEND": "rust",
            "JOBDIR": directory,
        },
        downloader=downloader,
    )
    result = await crawler.crawl()
    labels = [request.cb_kwargs["label"] for request in downloader.requests]
    assert labels == [
        "normal-json",
        "normal-form",
        "start-first",
        "start-second",
        "low",
    ]
    assert type(downloader.requests[0]) is JsonRequest
    assert downloader.requests[0].dumps_kwargs == {"sort_keys": False}
    assert type(downloader.requests[1]) is FormRequest
    assert result.items == tuple({"label": label} for label in labels)
    assert result.stats["scheduler/recovered"] == 5
    assert result.stats["jobdir/migrated/requests"] == 5
    assert result.stats["jobdir/migrated/fingerprints"] == 2
    assert result.stats["jobdir/migrated/spider_state"] is True
    assert crawler.spider is not None
    assert crawler.spider.state == {"seeded": True, "source": "scrapy", "processed": 5}

    for relative, contents in source_files.items():
        assert (directory / relative).read_bytes() == contents
    with sqlite3.connect(directory / "job.sqlite3") as connection:
        marker = connection.execute(
            "SELECT value FROM metadata WHERE key = 'scrapy_migration_version'"
        ).fetchone()
        imported_fingerprints = {
            row[0]
            for row in connection.execute(
                "SELECT fingerprint FROM fingerprints WHERE fingerprint IN (?, ?)",
                fingerprints,
            )
        }
    assert marker == (1,)
    assert imported_fingerprints == set(fingerprints)

    second = migrate_scrapy_jobdir(
        directory,
        ScrapyImportedSpider(),
        _scrapy_settings(),
    )
    assert second is None


def _verify_scrapy_queue_formats(root: Path) -> None:
    formats = (
        ("scrapy.squeues.PickleFifoDiskQueue", "pickle", "fifo"),
        ("scrapy.squeues.PickleLifoDiskQueue", "pickle", "lifo"),
        ("scrapy.squeues.MarshalFifoDiskQueue", "marshal", "fifo"),
        ("scrapy.squeues.MarshalLifoDiskQueue", "marshal", "lifo"),
    )
    for index, (queue_class, serialization, order) in enumerate(formats):
        directory = root / str(index)
        queue = directory / "requests.queue"
        queue.mkdir(parents=True)
        interrupted = directory / ".spideroxide-migration-interrupted.sqlite3"
        interrupted.write_bytes(b"partial")
        (queue / "active.json").write_text("[0]", encoding="utf-8")
        _write_scrapy_queue(
            queue / "0",
            [_scrapy_request(f"{serialization}-{order}", 0)],
            serialization=serialization,
            order=order,
        )
        migration = migrate_scrapy_jobdir(
            directory,
            ScrapyImportedSpider(),
            _scrapy_settings(
                SCHEDULER_DISK_QUEUE=queue_class,
                SCHEDULER_START_DISK_QUEUE=None,
            ),
        )
        assert migration is not None
        assert migration.requests == 1
        assert interrupted.read_bytes() == b"partial"
        with sqlite3.connect(directory / "job.sqlite3") as connection:
            payload = connection.execute("SELECT payload FROM requests").fetchone()
        assert payload is not None
        request = deserialize_request(payload[0], ScrapyImportedSpider())
        assert request.url.endswith(f"/{serialization}-{order}")


def _verify_scrapy_jobdir_rejections(root: Path) -> None:
    corrupt = root / "corrupt"
    queue = corrupt / "requests.queue"
    queue.mkdir(parents=True)
    (queue / "active.json").write_text("[0]", encoding="utf-8")
    (queue / "0").write_bytes(struct.pack(">L", 1))
    before = (queue / "0").read_bytes()
    try:
        migrate_scrapy_jobdir(corrupt, ScrapyImportedSpider(), _scrapy_settings())
    except ValueError as error:
        assert "truncated LIFO entry" in str(error)
    else:
        raise AssertionError("corrupt Scrapy queue was imported")
    assert not (corrupt / "job.sqlite3").exists()
    assert (queue / "0").read_bytes() == before

    unsupported = root / "unsupported"
    queue = unsupported / "requests.queue"
    queue.mkdir(parents=True)
    (queue / "active.json").write_text("[0]", encoding="utf-8")
    _write_scrapy_queue(
        queue / "0",
        [_scrapy_request("unsupported", 0)],
        serialization="pickle",
        order="lifo",
    )
    try:
        migrate_scrapy_jobdir(
            unsupported,
            ScrapyImportedSpider(),
            _scrapy_settings(SCHEDULER_DISK_QUEUE="project.CustomQueue"),
        )
    except ValueError as error:
        assert "unsupported SCHEDULER_DISK_QUEUE" in str(error)
    else:
        raise AssertionError("custom Scrapy queue was imported")
    assert not (unsupported / "job.sqlite3").exists()

    custom_request = root / "custom-request"
    queue = custom_request / "requests.queue"
    queue.mkdir(parents=True)
    (queue / "active.json").write_text("[0]", encoding="utf-8")
    request = _scrapy_request("custom", 0)
    request["_class"] = "project.requests.CustomRequest"
    _write_scrapy_queue(
        queue / "0",
        [request],
        serialization="pickle",
        order="lifo",
    )
    try:
        migrate_scrapy_jobdir(custom_request, ScrapyImportedSpider(), _scrapy_settings())
    except ValueError as error:
        assert "unsupported Scrapy persisted request class" in str(error)
    else:
        raise AssertionError("custom Scrapy request class was imported")
    assert not (custom_request / "job.sqlite3").exists()

    ambiguous = root / "ambiguous"
    coordinator = NativeCrawlCoordinator(1, 1, str(ambiguous))
    coordinator.close()
    (ambiguous / "spider.state").write_bytes(pickle.dumps({"source": "scrapy"}, protocol=4))
    try:
        migrate_scrapy_jobdir(ambiguous, ScrapyImportedSpider(), _scrapy_settings())
    except ValueError as error:
        assert "coexist without a migration marker" in str(error)
    else:
        raise AssertionError("ambiguous native and Scrapy state was accepted")


async def _verify() -> None:
    _verify_request_subclass_roundtrip()
    with tempfile.TemporaryDirectory(prefix="spideroxide-native-store-") as temporary:
        await _verify_native_store(Path(temporary))
    with tempfile.TemporaryDirectory(prefix="spideroxide-crawl-resume-") as temporary:
        await _verify_crawl_resume(Path(temporary))
    with tempfile.TemporaryDirectory(prefix="spideroxide-unserializable-") as temporary:
        await _verify_unserializable_fallback(Path(temporary))
    with tempfile.TemporaryDirectory(prefix="spideroxide-unfiltered-resume-") as temporary:
        await _verify_unfiltered_recovery(Path(temporary))
    with tempfile.TemporaryDirectory(prefix="spideroxide-hard-crash-") as temporary:
        await _verify_hard_crash(Path(temporary))
    with tempfile.TemporaryDirectory(prefix="spideroxide-close-resume-") as temporary:
        await _verify_graceful_close_resume(Path(temporary))
    with tempfile.TemporaryDirectory(prefix="spideroxide-python-jobdir-") as temporary:
        await _verify_python_rejection(Path(temporary))
    with tempfile.TemporaryDirectory(prefix="spideroxide-schema-") as temporary:
        _verify_schema_rejection(Path(temporary))
    with tempfile.TemporaryDirectory(prefix="spideroxide-schema-migration-") as temporary:
        _verify_schema_migration(Path(temporary) / "job")
    with tempfile.TemporaryDirectory(prefix="spideroxide-fingerprint-migration-") as temporary:
        _verify_fingerprint_schema_migration(Path(temporary) / "job")
    with tempfile.TemporaryDirectory(prefix="spideroxide-scrapy-resume-") as temporary:
        await _verify_scrapy_jobdir_resume(Path(temporary))
    with tempfile.TemporaryDirectory(prefix="spideroxide-scrapy-formats-") as temporary:
        _verify_scrapy_queue_formats(Path(temporary))
    with tempfile.TemporaryDirectory(prefix="spideroxide-scrapy-rejections-") as temporary:
        _verify_scrapy_jobdir_rejections(Path(temporary))


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--crash-worker":
        asyncio.run(_run_crash_worker(Path(sys.argv[2])))
    else:
        asyncio.run(_verify())
        print(
            "Persistent job state passed: Rust WAL storage, locking, recovery, priority, "
            "fingerprints, callbacks, request data, spider state, cancellation, graceful stops, "
            "hard crashes, memory fallback, schema checks, Scrapy JOBDIR migration, and "
            "auto-engine selection"
        )
