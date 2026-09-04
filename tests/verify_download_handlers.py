from __future__ import annotations

import asyncio
import ftplib
import sys
import tempfile
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from spideroxide import (
    BaseDownloadHandler,
    Crawler,
    DataURIDownloadHandler,
    DownloadHandlers,
    FileDownloadHandler,
    FTPDownloadHandler,
    HtmlResponse,
    NotConfigured,
    NotSupported,
    Request,
    Response,
    S3DownloadHandler,
    Settings,
    Spider,
    TextResponse,
    fingerprint_request,
)


class RecordingHandler(BaseDownloadHandler):
    lazy = True
    created = 0
    closed = 0
    requests: list[Request] = []

    def __init__(self, crawler: object) -> None:
        super().__init__(crawler)
        type(self).created += 1

    async def download_request(self, request: Request) -> Response:
        type(self).requests.append(request)
        return TextResponse(request.url, body=b"custom response")

    async def close(self) -> None:
        type(self).closed += 1


class EagerHandler(RecordingHandler):
    lazy = False


class S3RedirectHandler(BaseDownloadHandler):
    lazy = True
    requests: list[str] = []

    async def download_request(self, request: Request) -> Response:
        type(self).requests.append(request.url)
        if request.url.endswith("/folder/start.json"):
            return Response(
                request.url,
                status=301,
                headers={
                    "Location": ("https://example-bucket.s3.us-west-2.amazonaws.com/final.json")
                },
            )
        return TextResponse(request.url, body=b"redirected")


class LatencyHandler(BaseDownloadHandler):
    lazy = True

    async def download_request(self, request: Request) -> Response:
        request.meta["download_latency"] = 0.125
        return Response(request.url)


class DisabledHandler(BaseDownloadHandler):
    lazy = False

    def __init__(self, crawler: object) -> None:
        raise NotConfigured("disabled for verification")

    async def download_request(self, request: Request) -> Response:
        raise AssertionError("disabled handler should not download")


class CloseOnly:
    closed = 0

    async def close(self) -> None:
        type(self).closed += 1


class InvalidFactoryHandler(BaseDownloadHandler):
    lazy = False

    @classmethod
    def from_crawler(cls, crawler: object) -> CloseOnly:
        return CloseOnly()

    async def download_request(self, request: Request) -> Response:
        raise AssertionError("factory result should be validated")


class FailingCloseHandler(BaseDownloadHandler):
    async def download_request(self, request: Request) -> Response:
        return Response(request.url)

    async def close(self) -> None:
        raise RuntimeError("close failed")


class SuccessfulCloseHandler(BaseDownloadHandler):
    closed = 0

    async def download_request(self, request: Request) -> Response:
        return Response(request.url)

    async def close(self) -> None:
        type(self).closed += 1


class HandlerSpider(Spider):
    name = "download-handlers"

    def __init__(self, urls: list[str]) -> None:
        super().__init__()
        self.start_urls = urls

    def parse(self, response: Response) -> dict[str, object]:
        return {
            "url": response.url,
            "status": response.status,
            "body": response.body,
            "type": type(response).__name__,
            "request_attached": response.request is not None,
        }


class UnsupportedSpider(Spider):
    name = "unsupported-handler"

    def start_requests(self) -> object:
        yield Request(
            "unknown:resource",
            errback=self.failed,
            dont_filter=True,
        )

    def failed(self, error: BaseException) -> dict[str, str]:
        assert isinstance(error, NotSupported)
        return {"error": str(error)}


class ExplicitSlotSpider(Spider):
    name = "explicit-handler-slot"

    def start_requests(self) -> object:
        yield Request(
            "data:text/plain,slotted",
            meta={"download_slot": "local-handlers"},
        )

    def parse(self, response: Response) -> dict[str, bytes]:
        return {"body": response.body}


class FakeFTP:
    instances: list[FakeFTP] = []
    failure: str | None = None
    connect_error: OSError | None = None
    closes = 0

    def __init__(self) -> None:
        self.connected: tuple[str, int] | None = None
        self.credentials: tuple[str, str] | None = None
        self.passive: bool | None = None
        self.command: str | None = None
        type(self).instances.append(self)

    def connect(self, hostname: str, port: int) -> None:
        if self.connect_error is not None:
            raise self.connect_error
        self.connected = (hostname, port)

    def login(self, user: str, password: str) -> None:
        self.credentials = (user, password)

    def set_pasv(self, passive: bool) -> None:
        self.passive = passive

    def retrbinary(self, command: str, callback: object) -> None:
        self.command = command
        if self.failure is not None:
            error_type = ftplib.error_temp if self.failure.startswith("4") else ftplib.error_perm
            raise error_type(self.failure)
        callback(b"hello ")  # type: ignore[operator]
        callback(b"ftp")  # type: ignore[operator]

    def quit(self) -> None:
        return None

    def close(self) -> None:
        type(self).closes += 1


def _crawler(settings: dict[str, object]) -> SimpleNamespace:
    return SimpleNamespace(settings=Settings(settings), spider=SimpleNamespace())


async def _verify_manager() -> None:
    RecordingHandler.created = 0
    RecordingHandler.closed = 0
    RecordingHandler.requests.clear()
    S3RedirectHandler.requests.clear()
    EagerHandler.created = 0
    EagerHandler.closed = 0
    crawler = _crawler(
        {
            "DOWNLOAD_HANDLERS_BASE": {
                "eager": EagerHandler,
                "lazy": RecordingHandler,
                "disabled": DisabledHandler,
            },
            "DOWNLOAD_HANDLERS": {"eager": None},
        }
    )
    handlers = DownloadHandlers(crawler)
    assert EagerHandler.created == 0
    assert RecordingHandler.created == 0

    request = Request("lazy://example.test/resource")
    response = await handlers.fetch(request)
    assert response.body == b"custom response"
    assert response.request is request
    assert request.meta["download_latency"] >= 0
    assert RecordingHandler.created == 1
    request.meta["download_latency"] = 123.0
    assert await handlers.fetch(request) is not None
    assert request.meta["download_latency"] != 123.0
    assert RecordingHandler.created == 1

    try:
        DownloadHandlers(_crawler({"DOWNLOADER_BACKEND": "invalid"}))
    except ValueError as error:
        assert "invalid downloader backend" in str(error)
    else:
        raise AssertionError("invalid core downloader backend was swallowed")

    for url, reason in (
        ("unknown://example.test", "no handler available for that scheme"),
        ("disabled://example.test", "disabled for verification"),
        ("eager://example.test", "no handler available for that scheme"),
    ):
        try:
            await handlers.fetch(Request(url))
        except NotSupported as error:
            assert reason in str(error)
        else:
            raise AssertionError(f"{url} unexpectedly had a download handler")

    await handlers.close()
    await handlers.close()
    assert RecordingHandler.closed == 1

    CloseOnly.closed = 0
    with patch("spideroxide.downloadhandlers.logger.error") as logged:
        invalid = DownloadHandlers(
            _crawler(
                {
                    "DOWNLOAD_HANDLERS_BASE": {"invalid": InvalidFactoryHandler},
                    "DOWNLOAD_HANDLERS": {},
                }
            )
        )
    logged.assert_called_once()
    try:
        await invalid.fetch(Request("invalid://example.test"))
    except NotSupported as error:
        assert "download handler must define download_request()" in str(error)
    else:
        raise AssertionError("invalid factory result was accepted")
    await invalid.close()
    assert CloseOnly.closed == 1

    SuccessfulCloseHandler.closed = 0
    closing = DownloadHandlers(
        _crawler(
            {
                "DOWNLOAD_HANDLERS_BASE": {
                    "first": FailingCloseHandler,
                    "second": SuccessfulCloseHandler,
                },
                "DOWNLOAD_HANDLERS": {},
            }
        )
    )
    try:
        await closing.close()
    except RuntimeError as error:
        assert str(error) == "close failed"
    else:
        raise AssertionError("handler close failure was swallowed")
    assert SuccessfulCloseHandler.closed == 1


async def _verify_data_and_file() -> None:
    crawler = _crawler({"DOWNLOAD_HANDLERS_BASE": {}, "DOWNLOAD_HANDLERS": {}})
    data_handler = DataURIDownloadHandler(crawler)

    plain = await data_handler.download_request(Request("data:,hello%20world"))
    assert isinstance(plain, TextResponse)
    assert plain.text == "hello world"
    assert len(plain.headers) == 0

    encoded = await data_handler.download_request(
        Request("data:text/plain;charset=iso-8859-1,caf%E9")
    )
    assert isinstance(encoded, TextResponse)
    assert encoded.text == "café"
    json_encoded = await data_handler.download_request(
        Request("data:application/json;charset=iso-8859-1,%22caf%E9%22")
    )
    assert isinstance(json_encoded, TextResponse)
    assert json_encoded.text == '"café"'

    html = await data_handler.download_request(
        Request("data:text/html;base64,PGh0bWw+PGgxPk9LPC9oMT48L2h0bWw+")
    )
    assert isinstance(html, HtmlResponse)
    assert html.css("h1::text").get() == "OK"

    binary = await data_handler.download_request(
        Request("data:application/octet-stream;base64,AAEC")
    )
    assert type(binary) is Response
    assert binary.body == b"\x00\x01\x02"
    for request in (
        Request("data:text/plain,hello?b=2&a=1"),
        Request("file:///tmp/example%20file.txt"),
        Request("file:///tmp/a/../b"),
        Request("data:text/plain,a%2Fb"),
    ):
        assert fingerprint_request(request, backend="python") == fingerprint_request(
            request, backend="rust"
        )

    with tempfile.TemporaryDirectory(prefix="spideroxide-download-handler-") as temporary:
        html_path = Path(temporary) / "page with space.html"
        html_path.write_text("<html><title>Local</title></html>", encoding="utf-8")
        file_request = Request(html_path.as_uri())
        file_response = await FileDownloadHandler(crawler).download_request(file_request)
        assert isinstance(file_response, HtmlResponse)
        assert file_response.css("title::text").get() == "Local"


async def _verify_ftp() -> None:
    crawler = _crawler(
        {
            "DOWNLOAD_HANDLERS_BASE": {},
            "DOWNLOAD_HANDLERS": {},
            "FTP_USER": "default-user",
            "FTP_PASSWORD": "default-password",
            "FTP_PASSIVE_MODE": False,
        }
    )
    handler = FTPDownloadHandler(crawler)
    FakeFTP.instances.clear()
    FakeFTP.closes = 0
    FakeFTP.connect_error = None
    FakeFTP.failure = None
    with patch("spideroxide.downloadhandlers.ftplib.FTP", FakeFTP):
        request = Request(
            "ftp://files.example.test:2121/folder/file.txt",
            meta={"ftp_user": "request-user", "ftp_password": "request-password"},
        )
        response = await handler.download_request(request)
    assert isinstance(response, TextResponse)
    assert response.body == b"hello ftp"
    assert response.headers["Size"] == b"9"
    assert response.headers["Local Filename"] == b""
    ftp = FakeFTP.instances[-1]
    assert ftp.connected == ("files.example.test", 2121)
    assert ftp.credentials == ("request-user", "request-password")
    assert ftp.passive is False
    assert ftp.command == "RETR /folder/file.txt"

    with tempfile.TemporaryDirectory(prefix="spideroxide-ftp-") as temporary:
        destination = Path(temporary) / "download.txt"
        with patch("spideroxide.downloadhandlers.ftplib.FTP", FakeFTP):
            local = await handler.download_request(
                Request(
                    "ftp://files.example.test/file.txt",
                    meta={"ftp_local_filename": str(destination)},
                )
            )
        assert destination.read_bytes() == b"hello ftp"
        assert local.body == bytes(str(destination), "utf-8")
        assert local.headers["Local Filename"] == bytes(str(destination), "utf-8")

    FakeFTP.failure = "550 file unavailable"
    with patch("spideroxide.downloadhandlers.ftplib.FTP", FakeFTP):
        missing = await handler.download_request(Request("ftp://files.example.test/missing"))
    assert missing.status == 404
    assert missing.body == b"550 file unavailable"
    FakeFTP.failure = "450 file busy"
    with patch("spideroxide.downloadhandlers.ftplib.FTP", FakeFTP):
        temporary_failure = await handler.download_request(Request("ftp://files.example.test/busy"))
    assert temporary_failure.status == 503
    assert temporary_failure.body == b"450 file busy"
    FakeFTP.failure = None

    FakeFTP.connect_error = OSError("connection failed")
    with patch("spideroxide.downloadhandlers.ftplib.FTP", FakeFTP):
        try:
            await handler.download_request(Request("ftp://files.example.test/unreachable"))
        except OSError as error:
            assert str(error) == "connection failed"
        else:
            raise AssertionError("FTP connection error was replaced or swallowed")
    assert FakeFTP.closes == 1
    FakeFTP.connect_error = None


async def _verify_s3() -> None:
    botocore = ModuleType("botocore")
    auth = ModuleType("botocore.auth")
    credentials = ModuleType("botocore.credentials")
    auth.AUTH_TYPE_MAPS = {}  # type: ignore[attr-defined]
    botocore.auth = auth  # type: ignore[attr-defined]
    botocore.credentials = credentials  # type: ignore[attr-defined]

    RecordingHandler.requests.clear()
    crawler = _crawler(
        {
            "DOWNLOAD_HANDLERS_BASE": {
                "https": RecordingHandler,
                "s3": S3DownloadHandler,
            },
            "DOWNLOAD_HANDLERS": {},
        }
    )
    with patch.dict(
        sys.modules,
        {
            "botocore": botocore,
            "botocore.auth": auth,
            "botocore.credentials": credentials,
        },
    ):
        handler = S3DownloadHandler(crawler)
        response = await handler.download_request(
            Request("s3://example-bucket/folder/file.json?versionId=1")
        )
        await handler.close()
        latency_handler = S3DownloadHandler(
            _crawler(
                {
                    "DOWNLOAD_HANDLERS_BASE": {
                        "https": LatencyHandler,
                        "s3": S3DownloadHandler,
                    },
                    "DOWNLOAD_HANDLERS": {},
                }
            )
        )
        latency_request = Request("s3://example-bucket/latency")
        await latency_handler.download_request(latency_request)
        await latency_handler.close()
        redirected = await Crawler(
            HandlerSpider,
            {"DOWNLOAD_HANDLERS": {"https": S3RedirectHandler}},
        ).crawl(["s3://example-bucket/folder/start.json"])
    assert response.url == "s3://example-bucket/folder/file.json?versionId=1"
    assert RecordingHandler.requests[-1].url == (
        "https://example-bucket.s3.amazonaws.com/folder/file.json?versionId=1"
    )
    assert latency_request.meta["download_latency"] == 0.125
    assert response.follow("next.json").url == "s3://example-bucket/folder/next.json"
    assert handler._public_location(response.url, "../other.json") == (
        "s3://example-bucket/other.json"
    )
    assert (
        handler._public_location(
            response.url,
            "https://other-bucket.s3.us-east-1.amazonaws.com/object.json",
        )
        == "s3://other-bucket/object.json"
    )
    assert redirected.items == (
        {
            "url": "s3://example-bucket/final.json",
            "status": 200,
            "body": b"redirected",
            "type": "TextResponse",
            "request_attached": True,
        },
    )
    assert redirected.stats["redirect/count"] == 1
    assert S3RedirectHandler.requests == [
        "https://example-bucket.s3.amazonaws.com/folder/start.json",
        "https://example-bucket.s3.us-west-2.amazonaws.com/final.json",
    ]

    unavailable = DownloadHandlers(
        _crawler(
            {
                "DOWNLOAD_HANDLERS_BASE": {"s3": S3DownloadHandler},
                "DOWNLOAD_HANDLERS": {},
            }
        )
    )
    try:
        await unavailable.fetch(Request("s3://example-bucket/file"))
    except NotSupported as error:
        assert "missing botocore library" in str(error)
    else:
        raise AssertionError("S3 handler loaded without botocore")


async def _verify_engines() -> None:
    with tempfile.TemporaryDirectory(prefix="spideroxide-handler-engines-") as temporary:
        file_path = Path(temporary) / "local.txt"
        file_path.write_text("local file", encoding="utf-8")
        for engine in ("python", "rust"):
            RecordingHandler.created = 0
            RecordingHandler.closed = 0
            crawler = Crawler(
                HandlerSpider,
                {
                    "ENGINE_BACKEND": engine,
                    "DOWNLOAD_HANDLERS": {"fixture": RecordingHandler},
                },
            )
            result = await crawler.crawl(
                [
                    "data:text/plain,engine-data",
                    file_path.as_uri(),
                    "fixture://example.test/resource",
                ]
            )
            bodies = {item["body"] for item in result.items}
            assert bodies == {b"engine-data", b"local file", b"custom response"}
            assert all(item["request_attached"] for item in result.items)
            assert RecordingHandler.created == 1
            assert RecordingHandler.closed == 1
            assert result.stats["downloader/request_count"] == 3
            assert result.stats["downloader/response_count"] == 3

            unsupported = await Crawler(
                UnsupportedSpider,
                {"ENGINE_BACKEND": engine},
            ).crawl()
            assert unsupported.items == (
                {
                    "error": (
                        "Unsupported URL scheme 'unknown': no handler available for that scheme"
                    )
                },
            )
            if engine == "rust":
                slotted = await Crawler(
                    ExplicitSlotSpider,
                    {
                        "ENGINE_BACKEND": "rust",
                        "RANDOMIZE_DOWNLOAD_DELAY": False,
                    },
                ).crawl()
                assert slotted.items == ({"body": b"slotted"},)
                assert slotted.stats["downloader/slot/acquired"] == 1
                assert slotted.stats["downloader/slot/local-handlers/concurrency"] == 8
            assert unsupported.stats["downloader/exception_count"] == 1
            exception_key = "downloader/exception_type_count/spideroxide.exceptions.NotSupported"
            assert unsupported.stats[exception_key] == 1


async def _verify() -> None:
    await _verify_manager()
    await _verify_data_and_file()
    await _verify_ftp()
    await _verify_s3()
    await _verify_engines()


if __name__ == "__main__":
    asyncio.run(_verify())
    print(
        "Download handlers passed: dispatch, overrides, disabling, lazy loading, lifecycle, "
        "data URIs, files, FTP, S3, and Python/Rust engine parity"
    )
