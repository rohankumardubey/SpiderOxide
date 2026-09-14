from __future__ import annotations

import asyncio
import base64
import hashlib
import sys
import tempfile
from collections import Counter
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from ftplib import error_perm
from io import BytesIO
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from spideroxide._native import NativeMediaStore

from spideroxide import (
    Crawler,
    FilesPipeline,
    FTPFilesStore,
    GCSFilesStore,
    ImagesPipeline,
    Request,
    S3FilesStore,
    Spider,
)
from spideroxide.pipelines.media import MediaPipeline


def _image_bytes(size: tuple[int, int], *, alpha: bool = False) -> bytes:
    mode = "RGBA" if alpha else "RGB"
    color = (40, 90, 160, 128) if alpha else (40, 90, 160)
    image = Image.new(mode, size, color)
    output = BytesIO()
    image.save(output, "PNG")
    return output.getvalue()


IMAGE = _image_bytes((40, 20), alpha=True)
SMALL_IMAGE = _image_bytes((4, 4))
FILE_BODY = b"SpiderOxide media pipeline\n"


@asynccontextmanager
async def media_server():
    counts: Counter[str] = Counter()

    async def handle(
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        request_line = await reader.readline()
        while await reader.readline() not in {b"\r\n", b"\n", b""}:
            pass
        path = request_line.decode("latin-1").split()[1].split("?", 1)[0]
        counts[path] += 1
        if path == "/redirect":
            writer.write(
                b"HTTP/1.1 302 Found\r\n"
                b"Location: /file.txt\r\n"
                b"Content-Length: 0\r\n"
                b"Connection: close\r\n\r\n"
            )
        else:
            bodies = {
                "/page": b"<html><body>media</body></html>",
                "/file.txt": FILE_BODY,
                "/empty": b"",
                "/image.png": IMAGE,
                "/robots.txt": b"User-agent: *\nAllow: /\n",
                "/small.png": SMALL_IMAGE,
            }
            content_types = {
                "/page": "text/html",
                "/file.txt": "text/plain",
                "/empty": "application/octet-stream",
                "/image.png": "image/png",
                "/robots.txt": "text/plain",
                "/small.png": "image/png",
            }
            body = bodies.get(path, b"not found")
            status = b"200 OK" if path in bodies else b"404 Not Found"
            writer.write(
                b"HTTP/1.1 "
                + status
                + b"\r\nContent-Type: "
                + content_types.get(path, "text/plain").encode()
                + b"\r\nContent-Length: "
                + str(len(body)).encode()
                + b"\r\nConnection: close\r\n\r\n"
                + body
            )
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    host, port = server.sockets[0].getsockname()[:2]
    try:
        yield f"http://{host}:{port}", counts
    finally:
        server.close()
        await server.wait_closed()


class FilesSpider(Spider):
    name = "media-files"

    def __init__(self, base_url: str, *, include_failure: bool = True) -> None:
        super().__init__()
        self.base_url = base_url
        self.include_failure = include_failure

    async def start(self):
        yield Request(f"{self.base_url}/page", callback=self.parse)

    def parse(self, response):
        urls = [f"{self.base_url}/file.txt", f"{self.base_url}/file.txt"]
        if self.include_failure:
            urls.append(f"{self.base_url}/empty")
        return {"file_urls": urls}


class RedirectSpider(Spider):
    name = "media-redirect"

    def __init__(self, base_url: str) -> None:
        super().__init__()
        self.base_url = base_url

    async def start(self):
        yield Request(f"{self.base_url}/page", callback=self.parse)

    def parse(self, response):
        return {"file_urls": [f"{self.base_url}/redirect"]}


class ImagesSpider(Spider):
    name = "media-images"

    def __init__(self, base_url: str) -> None:
        super().__init__()
        self.base_url = base_url

    async def start(self):
        yield Request(f"{self.base_url}/page", callback=self.parse)

    def parse(self, response):
        return {
            "image_urls": [
                f"{self.base_url}/image.png",
                f"{self.base_url}/image.png",
                f"{self.base_url}/small.png",
            ]
        }


class CrossOriginFilesSpider(FilesSpider):
    name = "media-files-cross-origin"

    def parse(self, response):
        media_url = self.base_url.replace("127.0.0.1", "localhost")
        return {"file_urls": [f"{media_url}/file.txt"]}


class CustomFilesPipeline(FilesPipeline):
    pass


class FakeClientError(Exception):
    def __init__(self, code: str, status: int) -> None:
        self.response = {
            "Error": {"Code": code},
            "ResponseMetadata": {"HTTPStatusCode": status},
        }


class FakeS3Client:
    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], dict[str, object]] = {}

    def put_object(self, **kwargs: object) -> None:
        body = bytes(kwargs["Body"])
        self.objects[(str(kwargs["Bucket"]), str(kwargs["Key"]))] = {
            **kwargs,
            "Body": body,
            "ETag": f'"{hashlib.md5(body).hexdigest()}"',  # noqa: S324
            "LastModified": datetime.now(timezone.utc),
        }

    def head_object(self, *, Bucket: str, Key: str) -> dict[str, object]:  # noqa: N803
        try:
            return self.objects[(Bucket, Key)]
        except KeyError:
            raise FakeClientError("404", 404) from None


class FakeBlob:
    def __init__(self, name: str) -> None:
        self.name = name
        self.body = b""
        self.cache_control: str | None = None
        self.metadata: dict[str, str] = {}
        self.content_type: str | None = None
        self.predefined_acl: str | None = None
        self.updated = datetime.now(timezone.utc)
        self.md5_hash: str | None = None

    def upload_from_string(
        self,
        data: bytes,
        *,
        content_type: str,
        predefined_acl: str | None,
    ) -> None:
        self.body = data
        self.content_type = content_type
        self.predefined_acl = predefined_acl
        self.updated = datetime.now(timezone.utc)
        self.md5_hash = base64.b64encode(hashlib.md5(data).digest()).decode()  # noqa: S324


class FakeBucket:
    def __init__(self, name: str) -> None:
        self.name = name
        self.blobs: dict[str, FakeBlob] = {}

    def blob(self, name: str) -> FakeBlob:
        return self.blobs.setdefault(name, FakeBlob(name))

    def get_blob(self, name: str) -> FakeBlob | None:
        return self.blobs.get(name)


class FakeGCSClient:
    projects: list[object] = []
    buckets: dict[str, FakeBucket] = {}

    def __init__(self, *, project: object = None) -> None:
        self.projects.append(project)

    def bucket(self, name: str) -> FakeBucket:
        return self.buckets.setdefault(name, FakeBucket(name))


class FakeFTP:
    files: dict[str, bytes] = {}
    modified: dict[str, str] = {}
    directories: list[str] = []
    connections: list[tuple[str, int, str, str, bool]] = []

    def __init__(self) -> None:
        self.host = ""
        self.port = 0
        self.username = ""
        self.password = ""
        self.passive = True

    def __enter__(self) -> FakeFTP:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def connect(self, host: str, port: int) -> None:
        self.host = host
        self.port = port

    def login(self, username: str, password: str) -> None:
        self.username = username
        self.password = password
        self.connections.append((self.host, self.port, username, password, self.passive))

    def set_pasv(self, passive: bool) -> None:
        self.passive = passive
        if self.connections:
            host, port, username, password, _ = self.connections[-1]
            self.connections[-1] = (host, port, username, password, passive)

    def mkd(self, path: str) -> None:
        self.directories.append(path)

    def storbinary(self, command: str, file: BytesIO) -> None:
        path = command.removeprefix("STOR ")
        self.files[path] = file.read()
        self.modified[path] = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")

    def voidcmd(self, command: str) -> str:
        path = command.removeprefix("MDTM ")
        try:
            return f"213 {self.modified[path]}"
        except KeyError:
            raise error_perm("550 file unavailable") from None

    def retrbinary(self, command: str, callback: object) -> None:
        path = command.removeprefix("RETR ")
        callback(self.files[path])


def _settings(
    store: Path,
    *,
    engine: str,
    downloader: str,
    pipeline: str,
    **values: object,
) -> dict[str, object]:
    store_setting = "IMAGES_STORE" if pipeline.endswith("ImagesPipeline") else "FILES_STORE"
    return {
        "ENGINE_BACKEND": engine,
        "DOWNLOADER_BACKEND": downloader,
        "ITEM_PIPELINES": {pipeline: 100},
        store_setting: str(store),
        "RETRY_ENABLED": False,
        **values,
    }


async def _verify_files(
    base_url: str,
    counts: Counter[str],
    root: Path,
    engine: str,
    downloader: str,
) -> None:
    file_count = counts["/file.txt"]
    empty_count = counts["/empty"]
    settings = _settings(
        root,
        engine=engine,
        downloader=downloader,
        pipeline="spideroxide.pipelines.FilesPipeline",
    )
    first = await Crawler(FilesSpider, settings).crawl(base_url)
    files = first.items[0]["files"]
    assert len(files) == 2
    assert files[0] == files[1]
    assert files[0]["status"] == "downloaded"
    assert files[0]["checksum"] == hashlib.md5(FILE_BODY).hexdigest()  # noqa: S324
    assert counts["/file.txt"] == file_count + 1
    assert counts["/empty"] == empty_count + 1
    assert first.stats["file_count"] == 1
    assert first.stats["file_status_count/downloaded"] == 1
    stored = root / files[0]["path"]
    assert stored.read_bytes() == FILE_BODY

    fresh = await Crawler(FilesSpider, settings).crawl(base_url)
    fresh_files = fresh.items[0]["files"]
    assert [entry["status"] for entry in fresh_files] == ["uptodate", "uptodate"]
    assert counts["/file.txt"] == file_count + 1
    assert counts["/empty"] == empty_count + 2
    assert fresh.stats["file_count"] == 1
    assert fresh.stats["file_status_count/uptodate"] == 1


async def _verify_redirects(
    base_url: str,
    counts: Counter[str],
    root: Path,
) -> None:
    blocked = await Crawler(
        RedirectSpider,
        _settings(
            root / "blocked",
            engine="python",
            downloader="python",
            pipeline="spideroxide.pipelines.FilesPipeline",
        ),
    ).crawl(base_url)
    assert blocked.items[0]["files"] == []
    target_count = counts["/file.txt"]

    followed = await Crawler(
        RedirectSpider,
        _settings(
            root / "followed",
            engine="python",
            downloader="python",
            pipeline="spideroxide.pipelines.FilesPipeline",
            MEDIA_ALLOW_REDIRECTS=True,
        ),
    ).crawl(base_url)
    assert followed.items[0]["files"][0]["status"] == "downloaded"
    assert counts["/file.txt"] == target_count + 1


async def _verify_robots_reentrancy(base_url: str, root: Path) -> None:
    result = await asyncio.wait_for(
        Crawler(
            CrossOriginFilesSpider,
            _settings(
                root,
                engine="rust",
                downloader="rust",
                pipeline="spideroxide.pipelines.FilesPipeline",
                CONCURRENT_REQUESTS=1,
                ROBOTSTXT_OBEY=True,
            ),
        ).crawl(base_url, include_failure=False),
        timeout=10,
    )
    assert len(result.items[0]["files"]) == 1


async def _verify_images(
    base_url: str,
    counts: Counter[str],
    root: Path,
) -> None:
    image_count = counts["/image.png"]
    small_count = counts["/small.png"]
    result = await Crawler(
        ImagesSpider,
        _settings(
            root,
            engine="rust",
            downloader="rust",
            pipeline="spideroxide.pipelines.ImagesPipeline",
            IMAGES_MIN_WIDTH=10,
            IMAGES_MIN_HEIGHT=10,
            IMAGES_THUMBS={"small": (12, 12)},
        ),
    ).crawl(base_url)
    images = result.items[0]["images"]
    assert len(images) == 2
    assert images[0] == images[1]
    assert counts["/image.png"] == image_count + 1
    assert counts["/small.png"] == small_count + 1
    full_path = root / images[0]["path"]
    thumb_path = root / "thumbs" / "small" / full_path.name
    with Image.open(full_path) as full:
        assert full.format == "JPEG"
        assert full.mode == "RGB"
        assert full.size == (40, 20)
    with Image.open(thumb_path) as thumb:
        assert thumb.format == "JPEG"
        assert thumb.size == (12, 6)
    assert hashlib.md5(full_path.read_bytes()).hexdigest() == images[0]["checksum"]  # noqa: S324
    assert result.stats["file_count"] == 2


def _verify_native_store() -> None:
    with tempfile.TemporaryDirectory() as directory:
        store = NativeMediaStore(directory)
        checksum = store.persist("nested/file.bin", b"native")
        assert checksum == hashlib.md5(b"native").hexdigest()  # noqa: S324
        modified, stat_checksum = store.stat("nested/file.bin")
        assert modified > 0
        assert stat_checksum == checksum
        assert bytes(store.read("nested/file.bin")) == b"native"
        try:
            store.persist("../escape", b"blocked")
        except ValueError:
            pass
        else:
            raise AssertionError("NativeMediaStore accepted a parent path")


async def _verify_remote_stores() -> None:
    info = MediaPipeline.SpiderInfo(FilesSpider("https://example.test"))

    fake_s3 = FakeS3Client()
    boto3_module = ModuleType("boto3")
    boto3_session_module = ModuleType("boto3.session")
    boto3_session_module.Session = lambda: type(
        "FakeSession",
        (),
        {"client": lambda self, name, **kwargs: fake_s3},
    )()
    boto3_module.session = boto3_session_module
    botocore_module = ModuleType("botocore")
    botocore_exceptions_module = ModuleType("botocore.exceptions")
    botocore_exceptions_module.ClientError = FakeClientError
    botocore_module.exceptions = botocore_exceptions_module
    with patch.dict(
        sys.modules,
        {
            "boto3": boto3_module,
            "boto3.session": boto3_session_module,
            "botocore": botocore_module,
            "botocore.exceptions": botocore_exceptions_module,
        },
    ):
        crawler = Crawler(
            FilesSpider,
            {
                "FILES_STORE": "s3://media-bucket/prefix",
                "FILES_STORE_S3_ACL": "public-read",
                "AWS_ACCESS_KEY_ID": "access",
                "AWS_SECRET_ACCESS_KEY": "secret",
                "AWS_SESSION_TOKEN": "token",
                "AWS_ENDPOINT_URL": "https://s3.example.test",
                "AWS_REGION_NAME": "test-region",
                "AWS_USE_SSL": True,
                "AWS_VERIFY": False,
            },
        )
        store = FilesPipeline.from_crawler(crawler).store
        assert isinstance(store, S3FilesStore)
        assert await store.stat_file("full/missing.bin", info) == {}
        await store.persist_file(
            "full/file.bin",
            BytesIO(FILE_BODY),
            info,
            meta={"width": 40},
            headers={"Content-Type": "application/test"},
        )
        stored = fake_s3.objects[("media-bucket", "prefix/full/file.bin")]
        assert stored["ACL"] == "public-read"
        assert stored["CacheControl"] == "max-age=172800"
        assert stored["ContentType"] == "application/test"
        assert stored["Metadata"] == {"width": "40"}
        stat = await store.stat_file("full/file.bin", info)
        assert stat["checksum"] == hashlib.md5(FILE_BODY).hexdigest()  # noqa: S324
        assert stat["last_modified"] > 0

    google_module = ModuleType("google")
    google_cloud_module = ModuleType("google.cloud")
    google_storage_module = ModuleType("google.cloud.storage")
    google_storage_module.Client = FakeGCSClient
    google_cloud_module.storage = google_storage_module
    google_module.cloud = google_cloud_module
    FakeGCSClient.projects.clear()
    FakeGCSClient.buckets.clear()
    with patch.dict(
        sys.modules,
        {
            "google": google_module,
            "google.cloud": google_cloud_module,
            "google.cloud.storage": google_storage_module,
        },
    ):
        crawler = Crawler(
            ImagesSpider,
            {
                "IMAGES_STORE": "gs://media-bucket/images",
                "IMAGES_STORE_GCS_ACL": "publicRead",
                "GCS_PROJECT_ID": "project-id",
            },
        )
        store = ImagesPipeline.from_crawler(crawler).store
        assert isinstance(store, GCSFilesStore)
        assert FakeGCSClient.projects == ["project-id"]
        assert await store.stat_file("full/missing.jpg", info) == {}
        await store.persist_file(
            "full/image.jpg",
            BytesIO(IMAGE),
            info,
            meta={"width": 40, "height": 20},
            headers={"Content-Type": "image/jpeg"},
        )
        blob = FakeGCSClient.buckets["media-bucket"].blobs["images/full/image.jpg"]
        assert blob.body == IMAGE
        assert blob.cache_control == "max-age=172800"
        assert blob.content_type == "image/jpeg"
        assert blob.predefined_acl == "publicRead"
        assert blob.metadata == {"width": "40", "height": "20"}
        stat = await store.stat_file("full/image.jpg", info)
        assert stat["checksum"] == hashlib.md5(IMAGE).hexdigest()  # noqa: S324

    FakeFTP.files.clear()
    FakeFTP.modified.clear()
    FakeFTP.directories.clear()
    FakeFTP.connections.clear()
    with patch("spideroxide.pipelines.files.FTP", FakeFTP):
        crawler = Crawler(
            FilesSpider,
            {
                "FILES_STORE": "ftp://uri-user:uri%20password@ftp.example.test:2121/media",
                "FTP_USER": "setting-user",
                "FTP_PASSWORD": "setting-password",
                "FEED_STORAGE_FTP_ACTIVE": True,
            },
        )
        store = FilesPipeline.from_crawler(crawler).store
        assert isinstance(store, FTPFilesStore)
        assert await store.stat_file("full/missing.bin", info) == {}
        await store.persist_file("full/file.bin", BytesIO(FILE_BODY), info)
        assert FakeFTP.files["/media/full/file.bin"] == FILE_BODY
        assert FakeFTP.directories == ["/media", "/media/full"]
        assert FakeFTP.connections[-1] == (
            "ftp.example.test",
            2121,
            "uri-user",
            "uri password",
            False,
        )
        FakeFTP.modified["/media/full/file.bin"] += ".123"
        stat = await store.stat_file("full/file.bin", info)
        assert stat["checksum"] == hashlib.md5(FILE_BODY).hexdigest()  # noqa: S324
        assert stat["last_modified"] > 0


def _verify_custom_settings() -> None:
    with tempfile.TemporaryDirectory() as directory:
        crawler = Crawler(
            FilesSpider,
            {
                "FILES_STORE": directory,
                "CUSTOMFILESPIPELINE_MEDIA_ALLOW_REDIRECTS": True,
                "CUSTOMFILESPIPELINE_FILES_EXPIRES": 12,
            },
        )
        pipeline = CustomFilesPipeline.from_crawler(crawler)
        assert pipeline.allow_redirects is True
        assert pipeline.expires == 12


def _verify_scrapy_paths() -> None:
    try:
        from scrapy.http import Request as ScrapyRequest
        from scrapy.pipelines.files import FilesPipeline as ScrapyFilesPipeline
        from scrapy.pipelines.images import ImagesPipeline as ScrapyImagesPipeline
    except ImportError:
        return
    url = "https://example.test/assets/file.txt?version=2"
    request = Request(url)
    scrapy_request = ScrapyRequest(url)
    assert FilesPipeline.file_path(FilesPipeline.__new__(FilesPipeline), request) == (
        ScrapyFilesPipeline.file_path(
            ScrapyFilesPipeline.__new__(ScrapyFilesPipeline),
            scrapy_request,
        )
    )
    assert ImagesPipeline.file_path(ImagesPipeline.__new__(ImagesPipeline), request) == (
        ScrapyImagesPipeline.file_path(
            ScrapyImagesPipeline.__new__(ScrapyImagesPipeline),
            scrapy_request,
        )
    )


async def main() -> None:
    _verify_native_store()
    await _verify_remote_stores()
    _verify_custom_settings()
    _verify_scrapy_paths()
    async with media_server() as (base_url, counts):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for engine in ("python", "rust"):
                for downloader in ("python", "rust"):
                    await _verify_files(
                        base_url,
                        counts,
                        root / f"{engine}-{downloader}",
                        engine,
                        downloader,
                    )
            await _verify_redirects(base_url, counts, root / "redirects")
            await _verify_robots_reentrancy(base_url, root / "robots")
            await _verify_images(base_url, counts, root / "images")
    print("media pipelines verified")


if __name__ == "__main__":
    asyncio.run(main())
