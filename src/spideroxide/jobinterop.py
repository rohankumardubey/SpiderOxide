from __future__ import annotations

import io
import json
import marshal
import os
import pickle
import sqlite3
import struct
import tempfile
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Literal

from .job import (
    request_from_scrapy_dict,
    serialize_request,
    serialize_spider_state,
)
from .settings import Settings
from .spider import Spider

_NATIVE_SCHEMA_VERSION = 3
_MIGRATION_VERSION = 1
_MIGRATION_MARKER = "scrapy_migration_version"
_SCRAPY_PATHS = ("requests.queue", "requests.seen", "spider.state")
_PRIORITY_QUEUE = "scrapy.pqueues.ScrapyPriorityQueue"
_QUEUE_FORMATS: dict[str, tuple[Literal["pickle", "marshal"], Literal["fifo", "lifo"]]] = {
    "scrapy.squeues.PickleFifoDiskQueue": ("pickle", "fifo"),
    "scrapy.squeues.PickleLifoDiskQueue": ("pickle", "lifo"),
    "scrapy.squeues.MarshalFifoDiskQueue": ("marshal", "fifo"),
    "scrapy.squeues.MarshalLifoDiskQueue": ("marshal", "lifo"),
}


@dataclass(frozen=True, slots=True)
class ScrapyJobMigration:
    requests: int
    fingerprints: int
    spider_state: bool


@dataclass(frozen=True, slots=True)
class _ImportedRequest:
    priority: int
    is_start_request: bool
    payload: bytes


def _migration_error(path: Path, message: str) -> ValueError:
    return ValueError(f"cannot import Scrapy JOBDIR {path}: {message}")


def _scrapy_layout_exists(path: Path) -> bool:
    return any((path / name).exists() for name in _SCRAPY_PATHS)


def _native_migration_version(database: Path) -> int | None:
    try:
        with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as connection:
            row = connection.execute(
                "SELECT value FROM metadata WHERE key = ?",
                (_MIGRATION_MARKER,),
            ).fetchone()
    except sqlite3.Error:
        return None
    if row is None or type(row[0]) is not int:
        return None
    return row[0]


@contextmanager
def _jobdir_lock(path: Path):
    lock_path = path / ".spideroxide.lock"
    lock_file = lock_path.open("a+b")
    locked = False
    try:
        if os.name == "nt":
            import msvcrt

            if lock_file.seek(0, os.SEEK_END) == 0:
                lock_file.write(b"\0")
                lock_file.flush()
            lock_file.seek(0)
            try:
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as error:
                raise RuntimeError(f"JOBDIR is already in use: {path}") from error
            locked = True
        else:
            import fcntl

            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as error:
                raise RuntimeError(f"JOBDIR is already in use: {path}") from error
            locked = True
        yield
    finally:
        if locked and os.name == "nt":
            import msvcrt

            lock_file.seek(0)
            try:
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
        elif locked:
            import fcntl

            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        lock_file.close()


def _exact_pickle(payload: bytes, context: str) -> object:
    stream = io.BytesIO(payload)
    try:
        value = pickle.load(stream)
    except (
        AttributeError,
        EOFError,
        ImportError,
        ModuleNotFoundError,
        pickle.PickleError,
        TypeError,
        ValueError,
    ) as error:
        raise ValueError(f"{context} cannot be decoded as Pickle: {error}") from error
    if stream.read(1):
        raise ValueError(f"{context} contains trailing Pickle data")
    return value


def _exact_marshal(payload: bytes, context: str) -> object:
    stream = io.BytesIO(payload)
    try:
        value = marshal.load(stream)
    except (EOFError, TypeError, ValueError) as error:
        raise ValueError(f"{context} cannot be decoded as Marshal: {error}") from error
    if stream.read(1):
        raise ValueError(f"{context} contains trailing Marshal data")
    return value


def _read_lifo(path: Path) -> list[bytes]:
    payload = path.read_bytes()
    if len(payload) < 4:
        raise ValueError(f"{path.name} is shorter than its LIFO header")
    (count,) = struct.unpack(">L", payload[:4])
    cursor = len(payload)
    values: list[bytes] = []
    for _ in range(count):
        if cursor < 8:
            raise ValueError(f"{path.name} contains a truncated LIFO entry")
        (size,) = struct.unpack(">L", payload[cursor - 4 : cursor])
        start = cursor - 4 - size
        if start < 4:
            raise ValueError(f"{path.name} contains an invalid LIFO entry size")
        values.append(payload[start : cursor - 4])
        cursor = start
    if cursor != 4:
        raise ValueError(f"{path.name} contains unframed LIFO data")
    values.reverse()
    return values


def _fifo_integer(value: object, name: str, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise ValueError(f"FIFO queue {name} must be an integer >= {minimum}")
    return value


def _fifo_position(value: object, length: int, name: str) -> tuple[int, ...]:
    if not isinstance(value, list) or len(value) != length:
        raise ValueError(f"FIFO queue {name} position is invalid")
    return tuple(_fifo_integer(item, f"{name}[{index}]") for index, item in enumerate(value))


def _read_exact(stream: BinaryIO, size: int, context: str) -> bytes:
    payload = stream.read(size)
    if len(payload) != size:
        raise ValueError(f"{context} is truncated")
    return payload


def _read_fifo(path: Path) -> list[bytes]:
    info_path = path / "info.json"
    try:
        info = json.loads(info_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError(f"{path.name}/info.json is invalid: {error}") from error
    if not isinstance(info, Mapping):
        raise ValueError(f"{path.name}/info.json is not an object")
    chunksize = _fifo_integer(info.get("chunksize"), "chunksize", minimum=1)
    size = _fifo_integer(info.get("size"), "size")
    head_number, head_count = _fifo_position(info.get("head"), 2, "head")
    tail_number, tail_count, tail_offset = _fifo_position(info.get("tail"), 3, "tail")
    if head_count >= chunksize or tail_count >= chunksize:
        raise ValueError("FIFO queue positions exceed the configured chunk size")

    values: list[bytes] = []
    chunk_number = tail_number
    chunk_count = tail_count
    chunk_offset = tail_offset
    stream: BinaryIO | None = None
    try:
        for _ in range(size):
            if stream is None:
                chunk_path = path / f"q{chunk_number:05d}"
                try:
                    stream = chunk_path.open("rb")
                except OSError as error:
                    raise ValueError(f"FIFO queue chunk {chunk_path.name} is missing") from error
                stream.seek(chunk_offset)
            header = _read_exact(stream, 4, f"FIFO queue chunk q{chunk_number:05d} header")
            (payload_size,) = struct.unpack(">L", header)
            values.append(
                _read_exact(
                    stream,
                    payload_size,
                    f"FIFO queue chunk q{chunk_number:05d} entry",
                )
            )
            chunk_count += 1
            chunk_offset = stream.tell()
            if chunk_count == chunksize and chunk_number <= head_number:
                stream.close()
                stream = None
                chunk_number += 1
                chunk_count = 0
                chunk_offset = 0
    finally:
        if stream is not None:
            stream.close()
    if (chunk_number, chunk_count) != (head_number, head_count):
        raise ValueError("FIFO queue size does not match its head and tail positions")
    return values


def _queue_decoder(
    path: Path,
    queue_class: object,
    setting_name: str,
) -> tuple[Callable[[bytes, str], object], list[bytes]]:
    if not isinstance(queue_class, str) or queue_class not in _QUEUE_FORMATS:
        choices = ", ".join(sorted(_QUEUE_FORMATS))
        raise ValueError(
            f"unsupported {setting_name} value {queue_class!r}; expected one of {choices}"
        )
    serialization, order = _QUEUE_FORMATS[queue_class]
    decoder = _exact_pickle if serialization == "pickle" else _exact_marshal
    return decoder, _read_fifo(path) if order == "fifo" else _read_lifo(path)


def _read_request_queue(path: Path, spider: Spider, settings: Settings) -> list[_ImportedRequest]:
    if not path.exists():
        return []
    if not path.is_dir():
        raise ValueError("requests.queue is not a directory")
    priority_queue = settings.get("SCHEDULER_PRIORITY_QUEUE", _PRIORITY_QUEUE)
    if priority_queue != _PRIORITY_QUEUE:
        raise ValueError(
            f"unsupported SCHEDULER_PRIORITY_QUEUE value {priority_queue!r}; "
            f"expected {_PRIORITY_QUEUE}"
        )
    active_path = path / "active.json"
    if not active_path.exists():
        if any(path.iterdir()):
            raise ValueError("requests.queue contains data but active.json is missing")
        return []
    try:
        active = json.loads(active_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError(f"requests.queue/active.json is invalid: {error}") from error
    if (
        not isinstance(active, list)
        or any(type(priority) is not int for priority in active)
        or len(active) != len(set(active))
    ):
        raise ValueError("requests.queue/active.json must contain unique integer priorities")

    expected_names = {"active.json"}
    imported: list[_ImportedRequest] = []
    normal_class = settings.get("SCHEDULER_DISK_QUEUE")
    start_class = settings.get("SCHEDULER_START_DISK_QUEUE")
    for queue_priority in sorted(active):
        found = False
        for is_start_request, suffix, queue_class, setting_name in (
            (False, "", normal_class, "SCHEDULER_DISK_QUEUE"),
            (True, "s", start_class, "SCHEDULER_START_DISK_QUEUE"),
        ):
            queue_name = f"{queue_priority}{suffix}"
            queue_path = path / queue_name
            if not queue_path.exists():
                continue
            found = True
            expected_names.add(queue_name)
            if queue_class is None:
                raise ValueError(f"{queue_name} exists but {setting_name} is disabled")
            decoder, payloads = _queue_decoder(queue_path, queue_class, setting_name)
            for index, payload in enumerate(payloads):
                context = f"requests.queue/{queue_name} entry {index}"
                values = decoder(payload, context)
                request = request_from_scrapy_dict(values, spider)
                if request.priority != -queue_priority:
                    raise ValueError(
                        f"{context} has priority {request.priority}, expected {-queue_priority}"
                    )
                stored_start = bool(request.meta.get("is_start_request", False))
                if is_start_request and not stored_start:
                    raise ValueError(f"{context} is in a start queue without start metadata")
                imported.append(
                    _ImportedRequest(
                        priority=request.priority,
                        is_start_request=stored_start,
                        payload=serialize_request(request, spider),
                    )
                )
        if not found:
            raise ValueError(f"active priority {queue_priority} has no queue data")
    unexpected = sorted(entry.name for entry in path.iterdir() if entry.name not in expected_names)
    if unexpected:
        raise ValueError(f"requests.queue contains unsupported entries: {', '.join(unexpected)}")
    return imported


def _read_fingerprints(path: Path) -> list[bytes]:
    if not path.exists():
        return []
    payload = path.read_bytes()
    fingerprints: list[bytes] = []
    cursor = 0
    while cursor < len(payload):
        if len(payload) - cursor < 2:
            raise ValueError("requests.seen contains a truncated fingerprint header")
        size = int.from_bytes(payload[cursor : cursor + 2], "big")
        cursor += 2
        if size != 20:
            raise ValueError(f"requests.seen contains a {size} byte fingerprint; expected 20")
        if len(payload) - cursor < size:
            raise ValueError("requests.seen contains a truncated fingerprint")
        fingerprints.append(payload[cursor : cursor + size])
        cursor += size
    return fingerprints


def _read_spider_state(path: Path) -> bytes | None:
    if not path.exists():
        return None
    state = _exact_pickle(path.read_bytes(), "spider.state")
    if not isinstance(state, dict):
        raise ValueError("spider.state is not a dictionary")
    return serialize_spider_state(state)


def _write_native_store(
    path: Path,
    requests: list[_ImportedRequest],
    fingerprints: list[bytes],
    spider_state: bytes | None,
) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".spideroxide-migration-",
        suffix=".sqlite3",
        dir=path,
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        with sqlite3.connect(temporary) as connection:
            connection.execute("PRAGMA journal_mode = DELETE")
            connection.execute("PRAGMA synchronous = FULL")
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
                CREATE TABLE job_state (
                    key TEXT PRIMARY KEY,
                    payload BLOB NOT NULL
                );
                """
            )
            connection.executemany(
                "INSERT INTO metadata(key, value) VALUES (?, ?)",
                (
                    ("schema_version", _NATIVE_SCHEMA_VERSION),
                    (_MIGRATION_MARKER, _MIGRATION_VERSION),
                ),
            )
            connection.executemany(
                "INSERT INTO fingerprints(fingerprint) VALUES (?)",
                ((fingerprint,) for fingerprint in dict.fromkeys(fingerprints)),
            )
            connection.executemany(
                """
                INSERT INTO requests(request_id, sequence, priority, is_start, payload)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    (
                        request_id,
                        request_id,
                        str(request.priority),
                        request.is_start_request,
                        request.payload,
                    )
                    for request_id, request in enumerate(requests)
                ),
            )
            if spider_state is not None:
                connection.execute(
                    "INSERT INTO job_state(key, payload) VALUES ('spider', ?)",
                    (spider_state,),
                )
        with temporary.open("rb") as database_file:
            os.fsync(database_file.fileno())
        os.replace(temporary, path / "job.sqlite3")
        try:
            directory_descriptor = os.open(path, os.O_RDONLY)
        except OSError:
            return
        try:
            try:
                os.fsync(directory_descriptor)
            except OSError:
                pass
        finally:
            os.close(directory_descriptor)
    finally:
        if temporary.exists():
            temporary.unlink()


def migrate_scrapy_jobdir(
    path: str | os.PathLike[str],
    spider: Spider,
    settings: Settings,
) -> ScrapyJobMigration | None:
    directory = Path(path)
    if not directory.exists() or not _scrapy_layout_exists(directory):
        return None
    if not directory.is_dir():
        raise _migration_error(directory, "path is not a directory")

    with _jobdir_lock(directory):
        database = directory / "job.sqlite3"
        if database.exists():
            version = _native_migration_version(database)
            if version == _MIGRATION_VERSION:
                return None
            if version is None:
                raise _migration_error(
                    directory,
                    "native and Scrapy state coexist without a migration marker",
                )
            raise _migration_error(
                directory,
                f"unsupported Scrapy migration version {version}",
            )
        try:
            requests = _read_request_queue(directory / "requests.queue", spider, settings)
            fingerprints = _read_fingerprints(directory / "requests.seen")
            spider_state = _read_spider_state(directory / "spider.state")
            _write_native_store(directory, requests, fingerprints, spider_state)
        except (OSError, sqlite3.Error, ValueError) as error:
            raise _migration_error(directory, str(error)) from error
    return ScrapyJobMigration(
        requests=len(requests),
        fingerprints=len(set(fingerprints)),
        spider_state=spider_state is not None,
    )
