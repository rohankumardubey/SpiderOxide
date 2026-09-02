from __future__ import annotations

import asyncio
import bz2
import gzip
import json
import logging
import lzma
import sys
import tempfile
import threading
import time
import types
from ftplib import error_perm
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from spideroxide import (
    BlockingFeedStorage,
    Crawler,
    FTPFeedStorage,
    GCSFeedStorage,
    GzipPlugin,
    JsonLinesItemExporter,
    LZMAPlugin,
    PostProcessingManager,
    Request,
    Response,
    S3FeedStorage,
    Settings,
    Spider,
)
from spideroxide.feedexport import _format_uri_template, _ftp_makedirs_cwd, _ftp_store_file


class FeedDownloader:
    async def fetch(self, request: Request) -> Response:
        return Response(request.url, request=request)

    async def close(self) -> None:
        pass


class FailingClosePlugin:
    def __init__(self, file, feed_options: dict[str, object]) -> None:
        self.file = file

    def write(self, data: bytes) -> int:
        return self.file.write(data)

    def close(self) -> None:
        raise OSError("expected postprocessing close failure")


class RemoteFeedSpider(Spider):
    name = "remote-feed"
    start_urls = ["https://example.test/one", "https://example.test/two"]

    def parse(self, response: Response) -> dict[str, object]:
        return {
            "id": int(response.url.rsplit("/", 1)[1] == "two") + 1,
            "name": "café",
        }


class ManyFeedSpider(RemoteFeedSpider):
    name = "many-remote-feeds"
    start_urls = [f"https://example.test/{index}" for index in range(8)]

    def parse(self, response: Response) -> dict[str, object]:
        return {"id": int(response.url.rsplit("/", 1)[1])}


class RecordingBlockingStorage(BlockingFeedStorage):
    uploads: list[tuple[str, bytes, int, dict[str, object]]] = []

    def __init__(
        self,
        uri: str,
        *,
        feed_options: dict[str, object] | None = None,
    ) -> None:
        self.uri = uri
        self.feed_options = feed_options or {}

    def _store_in_thread(self, file) -> None:
        try:
            file.seek(0)
            self.uploads.append(
                (
                    self.uri,
                    file.read(),
                    threading.get_ident(),
                    self.feed_options,
                )
            )
        finally:
            file.close()


class SlowBlockingStorage(RecordingBlockingStorage):
    def _store_in_thread(self, file) -> None:
        time.sleep(0.2)
        super()._store_in_thread(file)


class BoundedBlockingStorage(RecordingBlockingStorage):
    lock = threading.Lock()
    active = 0
    max_active = 0

    def _store_in_thread(self, file) -> None:
        with self.lock:
            type(self).active += 1
            type(self).max_active = max(type(self).max_active, type(self).active)
        try:
            time.sleep(0.05)
            super()._store_in_thread(file)
        finally:
            with self.lock:
                type(self).active -= 1


async def _crawl(
    engine: str,
    feeds: dict[object, dict[str, object]],
    spider: type[Spider] = RemoteFeedSpider,
    **settings: object,
):
    crawler = Crawler(
        spider,
        {
            "CONCURRENT_REQUESTS": 1,
            "ENGINE_BACKEND": engine,
            "FEEDS": feeds,
            **settings,
        },
        downloader=FeedDownloader(),
    )
    result = await crawler.crawl()
    assert result.reason == "finished"
    return crawler


def _decoded_lines(content: bytes) -> list[dict[str, object]]:
    return [json.loads(line) for line in content.splitlines()]


async def _verify_postprocessing(engine: str, directory: Path) -> None:
    gzip_path = directory / "items.jl.gz"
    bz2_path = directory / "items.jl.bz2"
    lzma_path = directory / "items.jl.xz"
    await _crawl(
        engine,
        {
            gzip_path: {
                "format": "jsonlines",
                "overwrite": True,
                "postprocessing": [GzipPlugin],
                "gzip_compresslevel": 6,
                "gzip_mtime": 0,
                "gzip_filename": "",
            },
            bz2_path: {
                "format": "jsonlines",
                "overwrite": True,
                "postprocessing": ["spideroxide.feedpostprocessing.Bz2Plugin"],
                "bz2_compresslevel": 5,
            },
            lzma_path: {
                "format": "jsonlines",
                "overwrite": True,
                "postprocessing": [LZMAPlugin],
                "lzma_preset": 3,
            },
        },
    )
    expected = [{"id": 1, "name": "café"}, {"id": 2, "name": "café"}]
    assert _decoded_lines(gzip.decompress(gzip_path.read_bytes())) == expected
    assert _decoded_lines(bz2.decompress(bz2_path.read_bytes())) == expected
    assert _decoded_lines(lzma.decompress(lzma_path.read_bytes())) == expected


async def _verify_compressed_batches(engine: str, directory: Path) -> None:
    template = directory / "batch-%(batch_id)02d.jl.gz"
    crawler = await _crawl(
        engine,
        {
            template: {
                "format": "jsonlines",
                "overwrite": True,
                "batch_item_count": 1,
                "postprocessing": [GzipPlugin],
                "gzip_mtime": 0,
                "gzip_filename": "",
            }
        },
    )
    first = _decoded_lines(gzip.decompress((directory / "batch-01.jl.gz").read_bytes()))
    second = _decoded_lines(gzip.decompress((directory / "batch-02.jl.gz").read_bytes()))
    assert first == [{"id": 1, "name": "café"}]
    assert second == [{"id": 2, "name": "café"}]
    assert crawler.stats.get_value("feedexport/success_count/FileFeedStorage") == 2


async def _verify_blocking_storage(engine: str, directory: Path) -> None:
    RecordingBlockingStorage.uploads.clear()
    main_thread = threading.get_ident()
    crawler = await _crawl(
        engine,
        {
            "record://bucket/items.jl.gz": {
                "format": "jsonlines",
                "overwrite": True,
                "postprocessing": [GzipPlugin],
                "gzip_mtime": 0,
                "gzip_filename": "",
            }
        },
        FEED_STORAGES={"record": RecordingBlockingStorage},
        FEED_TEMPDIR=str(directory),
    )
    assert len(RecordingBlockingStorage.uploads) == 1
    uri, content, worker_thread, options = RecordingBlockingStorage.uploads[0]
    assert uri == "record://bucket/items.jl.gz"
    assert worker_thread != main_thread
    assert options["overwrite"] is True
    assert _decoded_lines(gzip.decompress(content)) == [
        {"id": 1, "name": "café"},
        {"id": 2, "name": "café"},
    ]
    assert list(directory.iterdir()) == []
    assert crawler.stats.get_value("feedexport/success_count/RecordingBlockingStorage") == 1


async def _verify_postprocessing_failure(engine: str, directory: Path) -> None:
    previous_disable = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    try:
        crawler = await _crawl(
            engine,
            {
                directory / "failed.jl": {
                    "format": "jsonlines",
                    "overwrite": True,
                    "postprocessing": [FailingClosePlugin],
                }
            },
        )
    finally:
        logging.disable(previous_disable)
    assert crawler.stats.get_value("feedexport/failed_count/FileFeedStorage") == 1
    assert crawler.stats.get_value("feedexport/success_count/FileFeedStorage") is None


async def _verify_parallel_uploads(engine: str, directory: Path) -> None:
    RecordingBlockingStorage.uploads.clear()
    started = asyncio.get_running_loop().time()
    await _crawl(
        engine,
        {
            "slow://bucket/first.jl": {
                "format": "jsonlines",
                "overwrite": True,
            },
            "slow://bucket/second.jl": {
                "format": "jsonlines",
                "overwrite": True,
            },
        },
        FEED_STORAGES={"slow": SlowBlockingStorage},
        FEED_TEMPDIR=str(directory),
    )
    elapsed = asyncio.get_running_loop().time() - started
    assert len(RecordingBlockingStorage.uploads) == 2
    assert elapsed < 0.35, f"remote uploads were serialized ({elapsed:.3f}s)"


async def _verify_upload_backpressure(engine: str, directory: Path) -> None:
    RecordingBlockingStorage.uploads.clear()
    BoundedBlockingStorage.active = 0
    BoundedBlockingStorage.max_active = 0
    await _crawl(
        engine,
        {
            "bounded://bucket/batch-%(batch_id)02d.jl": {
                "format": "jsonlines",
                "overwrite": True,
                "batch_item_count": 1,
            }
        },
        spider=ManyFeedSpider,
        CONCURRENT_REQUESTS=4,
        FEED_STORAGE_CONCURRENCY=2,
        FEED_STORAGES={"bounded": BoundedBlockingStorage},
        FEED_TEMPDIR=str(directory),
    )
    assert len(RecordingBlockingStorage.uploads) == 8
    assert BoundedBlockingStorage.max_active == 2
    assert BoundedBlockingStorage.active == 0


def _verify_manager_file_api() -> None:
    output = BytesIO()
    manager = PostProcessingManager(
        [GzipPlugin],
        output,
        {
            "gzip_compresslevel": 1,
            "gzip_mtime": 0,
            "gzip_filename": "",
        },
    )
    exporter = JsonLinesItemExporter(manager, encoding="utf-8")
    exporter.export_item({"value": 1})
    exporter.finish_exporting()
    assert manager.tell() > 0
    manager.close()
    manager.close()
    assert gzip.decompress(output.getvalue()) == b'{"value": 1}\n'


class FirstPlugin:
    def __init__(self, file, feed_options: dict[str, object]) -> None:
        self.file = file

    def write(self, data: bytes) -> int:
        return self.file.write(b"first(" + data + b")")

    def close(self) -> None:
        pass


class SecondPlugin:
    def __init__(self, file, feed_options: dict[str, object]) -> None:
        self.file = file

    def write(self, data: bytes) -> int:
        return self.file.write(b"second(" + data + b")")

    def close(self) -> None:
        pass


def _verify_plugin_order() -> None:
    output = BytesIO()
    manager = PostProcessingManager([FirstPlugin, SecondPlugin], output, {})
    assert manager.write(b"feed") == len(b"second(first(feed))")
    manager.close()
    assert output.getvalue() == b"second(first(feed))"


def _verify_plugin_stacks() -> None:
    empty_output = BytesIO()
    empty_manager = PostProcessingManager([], empty_output, {})
    empty_manager.write(b"unchanged")
    empty_manager.close()
    assert not empty_output.closed
    assert empty_output.getvalue() == b"unchanged"

    cases = [
        (
            [GzipPlugin, "spideroxide.feedpostprocessing.Bz2Plugin"],
            bz2.decompress,
        ),
        ([GzipPlugin, LZMAPlugin], lzma.decompress),
    ]
    for plugins, outer_decompress in cases:
        output = BytesIO()
        manager = PostProcessingManager(
            plugins,
            output,
            {"gzip_mtime": 0, "gzip_filename": ""},
        )
        manager.write(b"stacked compression")
        manager.close()
        assert gzip.decompress(outer_decompress(output.getvalue())) == b"stacked compression"


def _verify_uri_formatting() -> None:
    template = "ftp://user:p%40ss@example.test/feeds/a%20b-%(batch_id)02d.jl"
    assert _format_uri_template(template, {"batch_id": 3}) == (
        "ftp://user:p%40ss@example.test/feeds/a%20b-03.jl"
    )
    assert _format_uri_template("s3://bucket/%(batch_id)+#08x.jl", {"batch_id": 15}) == (
        "s3://bucket/+0x0000f.jl"
    )


def _verify_temporary_directory() -> None:
    with tempfile.TemporaryDirectory() as directory:
        storage = RecordingBlockingStorage("record://bucket/feed")
        spider = SimpleNamespace(
            crawler=SimpleNamespace(settings=Settings({"FEED_TEMPDIR": directory}))
        )
        file = storage.open(spider)
        assert Path(file.name).parent == Path(directory)
        assert Path(file.name).name.startswith("feed-")
        file.close()

        invalid = Path(directory) / "missing"
        spider.crawler.settings["FEED_TEMPDIR"] = str(invalid)
        try:
            storage.open(spider)
        except OSError as error:
            assert str(error) == f"Not a Directory: {invalid}"
        else:
            raise AssertionError("BlockingFeedStorage accepted a missing FEED_TEMPDIR")


def _verify_scrapy_postprocessing() -> None:
    try:
        from scrapy.extensions.postprocessing import (
            Bz2Plugin as ScrapyBz2Plugin,
        )
        from scrapy.extensions.postprocessing import (
            GzipPlugin as ScrapyGzipPlugin,
        )
        from scrapy.extensions.postprocessing import (
            LZMAPlugin as ScrapyLZMAPlugin,
        )
        from scrapy.extensions.postprocessing import (
            PostProcessingManager as ScrapyPostProcessingManager,
        )
    except ImportError:
        return

    cases = [
        (
            GzipPlugin,
            ScrapyGzipPlugin,
            {"gzip_compresslevel": 4, "gzip_mtime": 0, "gzip_filename": ""},
        ),
        (
            "spideroxide.feedpostprocessing.Bz2Plugin",
            ScrapyBz2Plugin,
            {"bz2_compresslevel": 3},
        ),
        (LZMAPlugin, ScrapyLZMAPlugin, {"lzma_preset": 2}),
    ]
    for plugin, scrapy_plugin, options in cases:
        output = BytesIO()
        manager = PostProcessingManager([plugin], output, options)
        manager.write(b"SpiderOxide feed parity\n")
        manager.close()

        scrapy_output = BytesIO()
        scrapy_manager = ScrapyPostProcessingManager(
            [scrapy_plugin],
            scrapy_output,
            options,
        )
        scrapy_manager.write(b"SpiderOxide feed parity\n")
        scrapy_manager.close()
        assert output.getvalue() == scrapy_output.getvalue()


class FakeS3Client:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def upload_fileobj(self, **kwargs: object) -> None:
        file = kwargs["Fileobj"]
        kwargs["content"] = file.read()
        self.calls.append(kwargs)


class FakeBotoSession:
    client_kwargs = None
    client_instance = FakeS3Client()

    def client(self, service: str, **kwargs: object) -> FakeS3Client:
        assert service == "s3"
        type(self).client_kwargs = kwargs
        return self.client_instance


def _verify_s3_configuration() -> None:
    boto3 = types.ModuleType("boto3")
    session_module = types.ModuleType("boto3.session")
    session_module.Session = FakeBotoSession
    boto3.session = session_module
    with patch.dict(
        sys.modules,
        {
            "boto3": boto3,
            "boto3.session": session_module,
        },
    ):
        storage = S3FeedStorage(
            "s3://uri-access:uri-secret@bucket/feeds/items.jl",
            access_key="setting-access",
            secret_key="setting-secret",
            session_token="token",
            endpoint_url="https://s3.example.test",
            region_name="test-1",
            acl="private",
        )
    assert storage.bucketname == "bucket"
    assert storage.keyname == "feeds/items.jl"
    assert storage.access_key == "uri-access"
    assert storage.secret_key == "uri-secret"
    assert FakeBotoSession.client_kwargs == {
        "aws_access_key_id": "uri-access",
        "aws_secret_access_key": "uri-secret",
        "aws_session_token": "token",
        "endpoint_url": "https://s3.example.test",
        "region_name": "test-1",
    }


def _verify_s3_upload() -> None:
    storage = S3FeedStorage.__new__(S3FeedStorage)
    storage.bucketname = "bucket"
    storage.keyname = "feeds/items.jl"
    storage.acl = "private"
    storage.s3_client = FakeS3Client()
    file = BytesIO(b"s3 feed")
    storage._store_in_thread(file)
    assert file.closed
    assert storage.s3_client.calls == [
        {
            "Bucket": "bucket",
            "Key": "feeds/items.jl",
            "Fileobj": file,
            "ExtraArgs": {"ACL": "private"},
            "content": b"s3 feed",
        }
    ]


class FakeBlob:
    def __init__(self) -> None:
        self.content = b""
        self.acl = None

    def upload_from_file(self, file, *, predefined_acl: str | None) -> None:
        self.content = file.read()
        self.acl = predefined_acl


class FakeBucket:
    def __init__(self) -> None:
        self.name = ""
        self.blob_name = ""
        self.created_blob = FakeBlob()

    def blob(self, name: str) -> FakeBlob:
        self.blob_name = name
        return self.created_blob


class FakeGCSClient:
    instance = None

    def __init__(self, *, project: str | None) -> None:
        self.project = project
        self.bucket = FakeBucket()
        type(self).instance = self

    def get_bucket(self, name: str) -> FakeBucket:
        self.bucket.name = name
        return self.bucket


def _verify_gcs_upload() -> None:
    google = types.ModuleType("google")
    cloud = types.ModuleType("google.cloud")
    storage_module = types.ModuleType("google.cloud.storage")
    storage_module.Client = FakeGCSClient
    google.cloud = cloud
    cloud.storage = storage_module
    storage = GCSFeedStorage(
        "gs://bucket/feeds/items.jl",
        "project",
        "authenticatedRead",
    )
    file = BytesIO(b"gcs feed")
    with patch.dict(
        sys.modules,
        {
            "google": google,
            "google.cloud": cloud,
            "google.cloud.storage": storage_module,
        },
    ):
        storage._store_in_thread(file)
    assert file.closed
    client = FakeGCSClient.instance
    assert client.project == "project"
    assert client.bucket.name == "bucket"
    assert client.bucket.blob_name == "feeds/items.jl"
    assert client.bucket.created_blob.content == b"gcs feed"
    assert client.bucket.created_blob.acl == "authenticatedRead"


class FakeFTP:
    instance = None

    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []
        type(self).instance = self

    def __enter__(self):
        return self

    def __exit__(self, *args: object) -> None:
        pass

    def connect(self, host: str, port: int) -> None:
        self.calls.append(("connect", host, port))

    def login(self, username: str, password: str) -> None:
        self.calls.append(("login", username, password))

    def set_pasv(self, value: bool) -> None:
        self.calls.append(("set_pasv", value))

    def cwd(self, path: str) -> None:
        self.calls.append(("cwd", path))

    def mkd(self, path: str) -> None:
        self.calls.append(("mkd", path))

    def storbinary(self, command: str, file) -> None:
        self.calls.append(("storbinary", command, file.read()))


class RacingDirectoryFTP:
    def __init__(self) -> None:
        self.directory_exists = False
        self.cwd_calls = []

    def cwd(self, path: str) -> None:
        self.cwd_calls.append(path)
        if path == "/feeds" and not self.directory_exists:
            raise error_perm("missing")

    def mkd(self, path: str) -> None:
        self.directory_exists = True
        raise error_perm("already exists")


def _verify_ftp_upload() -> None:
    storage = FTPFeedStorage(
        "ftp://user:p%40ss@example.test:2121/feeds/items.jl",
        use_active_mode=True,
        feed_options={"overwrite": False},
    )
    assert storage.password == "p@ss"
    assert storage.overwrite is False
    file = BytesIO(b"ftp feed")
    with patch("spideroxide.feedexport.FTP", FakeFTP):
        _ftp_store_file(
            path=storage.path,
            file=file,
            host=storage.host,
            port=storage.port,
            username=storage.username,
            password=storage.password,
            use_active_mode=storage.use_active_mode,
            overwrite=storage.overwrite,
        )
    assert file.closed
    assert FakeFTP.instance.calls == [
        ("connect", "example.test", 2121),
        ("login", "user", "p@ss"),
        ("set_pasv", False),
        ("cwd", "/feeds"),
        ("storbinary", "APPE items.jl", b"ftp feed"),
    ]
    racing = RacingDirectoryFTP()
    _ftp_makedirs_cwd(racing, "/feeds")
    assert racing.cwd_calls == ["/feeds", "/", "/feeds"]


async def main() -> None:
    _verify_manager_file_api()
    _verify_plugin_order()
    _verify_plugin_stacks()
    _verify_uri_formatting()
    _verify_temporary_directory()
    _verify_scrapy_postprocessing()
    _verify_s3_configuration()
    _verify_s3_upload()
    _verify_gcs_upload()
    _verify_ftp_upload()
    for engine in ("python", "rust"):
        with tempfile.TemporaryDirectory(prefix=f"spideroxide-remote-{engine}-") as temporary:
            directory = Path(temporary)
            await _verify_postprocessing(engine, directory / "postprocessing")
            await _verify_compressed_batches(engine, directory / "batches")
            temp_directory = directory / "temporary"
            temp_directory.mkdir()
            await _verify_blocking_storage(engine, temp_directory)
            await _verify_parallel_uploads(engine, temp_directory)
            await _verify_upload_backpressure(engine, temp_directory)
            await _verify_postprocessing_failure(engine, directory)
    print("remote feed storage and postprocessing verified")


if __name__ == "__main__":
    asyncio.run(main())
