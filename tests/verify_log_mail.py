from __future__ import annotations

import asyncio
import logging
import sys
import warnings
from email import message_from_string
from io import BytesIO
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from spideroxide import Crawler, DropItem, Request, Response, Settings, Spider
from spideroxide.exceptions import ScrapyDeprecationWarning
from spideroxide.logformatter import (
    CRAWLEDMSG,
    DOWNLOADERRORMSG_LONG,
    DOWNLOADERRORMSG_SHORT,
    LogFormatter,
    logformatter_adapter,
)
from spideroxide.logutils import (
    LogCounterHandler,
    SpiderLoggerAdapter,
    StreamLogger,
    TopLevelFormatter,
    _get_handler,
    _uninstall_scrapy_root_handler,
    configure_logging,
    get_scrapy_root_handler,
    install_scrapy_root_handler,
    log_scrapy_info,
)
from spideroxide.mail import MailSender, _to_bytes_or_none


class FixtureDownloader:
    async def fetch(self, request: Request) -> object:
        from spideroxide import TextResponse

        if request.url.endswith("/fail"):
            raise OSError("connection failed")
        return TextResponse(request.url, body=b"ok", request=request)

    async def close(self) -> None:
        pass


class FixturePipeline:
    def process_item(self, item: dict[str, str], spider: Spider) -> dict[str, str]:
        if item["kind"] == "drop":
            raise DropItem("fixture dropped", log_level="INFO")
        if item["kind"] == "error":
            raise ValueError("fixture error")
        return item


class FixtureSpider(Spider):
    name = "log-mail-fixture"
    start_urls = ["https://example.test/start", "https://example.test/fail"]

    def parse(self, response: object) -> list[object]:
        return [{"kind": "scraped"}, {"kind": "drop"}, {"kind": "error"}]


class FixtureFormatter(LogFormatter):
    @classmethod
    def from_crawler(cls, crawler: Crawler) -> FixtureFormatter:
        assert crawler.settings["LOG_FORMATTER"] == "__main__.FixtureFormatter"
        return cls()

    def scraped(self, item: object, response: object, spider: object) -> dict[str, object]:
        return {"level": logging.INFO, "msg": "formatted %(kind)s", "args": item}


class QuietFormatter(LogFormatter):
    def crawled(self, request: object, response: object, spider: object) -> None:
        return None


class CaptureHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


def verify_formatter_api() -> None:
    formatter = LogFormatter()
    spider = FixtureSpider()
    settings = Settings({"DEFAULT_DROPITEM_LOG_LEVEL": "WARNING"})
    spider.crawler = type("FormatterCrawler", (), {"settings": settings})()
    request = Request(
        "https://example.test/path",
        headers={"Referer": b"https://example.test/caf\xc3\xa9/\xff"},
        flags=["request-flag"],
    )
    response = Response(request.url, status=202, flags=["response-flag"])
    crawled = formatter.crawled(request, response, spider)
    assert crawled["msg"] == CRAWLEDMSG
    assert crawled["args"]["referer"] == "https://example.test/café/�"
    assert crawled["args"]["request_flags"] == " ['request-flag']"
    assert crawled["args"]["response_flags"] == " ['response-flag']"
    assert crawled["args"]["flags"] == crawled["args"]["response_flags"]
    assert formatter.scraped({"ok": True}, None, spider)["args"]["src"].endswith(
        ".FixtureSpider.start"
    )
    assert formatter.scraped("item", response, spider)["args"]["src"] is response
    assert (
        formatter.dropped("item", DropItem("dropped"), response, spider)["level"] == logging.WARNING
    )
    assert (
        formatter.dropped("item", DropItem("dropped", log_level="INFO"), response, spider)["level"]
        == logging.INFO
    )
    assert formatter.item_error("item", ValueError(), response, spider)["level"] == logging.ERROR
    assert formatter.spider_error(ValueError(), request, response, spider)["args"]["referer"] == (
        "https://example.test/café/�"
    )
    assert formatter.download_error(ValueError(), request, spider)["msg"] == DOWNLOADERRORMSG_SHORT
    assert formatter.download_error(ValueError(), request, spider, "oops")["msg"] == (
        DOWNLOADERRORMSG_LONG
    )
    assert LogFormatter.from_crawler(spider.crawler).__class__ is LogFormatter
    assert logformatter_adapter({"level": logging.DEBUG, "msg": "%s: %s", "args": ("a", "b")}) == (
        logging.DEBUG,
        "%s: %s",
        "a",
        "b",
    )
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        adapted = logformatter_adapter({"msg": "legacy %(item)s", "item": "hello"})
    assert adapted == (logging.INFO, "legacy %(item)s", {"msg": "legacy %(item)s", "item": "hello"})
    assert len(caught) == 1 and caught[0].category is ScrapyDeprecationWarning
    assert logformatter_adapter({"msg": ""}) == (logging.INFO, "")


def verify_logging_utils() -> None:
    record = logging.LogRecord(
        "scrapy.middlewares.foo", logging.WARNING, __file__, 1, "test", (), None
    )
    TopLevelFormatter(["scrapy"]).filter(record)
    assert record.name == "scrapy"
    adapter = SpiderLoggerAdapter(logging.getLogger("fixture"), {"spider": "fixture"})
    extra = {"source": "test"}
    _, kwargs = adapter.process("hello", {"extra": extra})
    assert kwargs["extra"] is extra and extra == {"source": "test", "spider": "fixture"}
    assert adapter.process("hello", {})[1]["extra"] == {"spider": "fixture"}

    handler = CaptureHandler()
    sample_logger = logging.getLogger("log-mail-fixture")
    old_level, old_propagate = sample_logger.level, sample_logger.propagate
    sample_logger.addHandler(handler)
    sample_logger.setLevel(logging.DEBUG)
    sample_logger.propagate = False
    try:
        StreamLogger(sample_logger).write(" one \n two \n")
        assert [entry.getMessage() for entry in handler.records] == [" one", " two"]
    finally:
        sample_logger.removeHandler(handler)
        sample_logger.setLevel(old_level)
        sample_logger.propagate = old_propagate
    log_handler = CaptureHandler()
    info_logger = logging.getLogger("spideroxide.logutils")
    info_logger.addHandler(log_handler)
    old_info_level, old_info_propagate = info_logger.level, info_logger.propagate
    info_logger.setLevel(logging.INFO)
    info_logger.propagate = False
    try:
        log_scrapy_info(Settings({"BOT_NAME": "mybot", "LOG_VERSIONS": ["Python", "libxml2"]}))
        assert len(log_handler.records) == 2
        assert "started (bot: mybot)" in log_handler.records[0].getMessage()
        assert log_handler.records[0].args["bot"] == "mybot"
        assert "Versions:\n{'Python': " in log_handler.records[1].getMessage()
        log_handler.records.clear()
        log_scrapy_info(Settings({"LOG_VERSIONS": []}))
        assert len(log_handler.records) == 1
        log_handler.records.clear()
        log_scrapy_info(Settings())
        assert len(log_handler.records) == 2
        assert "'libxml2':" in log_handler.records[1].getMessage()
    finally:
        info_logger.removeHandler(log_handler)
        info_logger.setLevel(old_info_level)
        info_logger.propagate = old_info_propagate

    scrapy = ModuleType("scrapy")
    scrapy.__version__ = "2.19.0"
    scrapy.spideroxide_compat_version = "0.1.0"
    previous = sys.modules.get("scrapy")
    compat_logger = logging.getLogger("scrapy.utils.log")
    compat_handler = CaptureHandler()
    old_compat_level, old_compat_propagate = compat_logger.level, compat_logger.propagate
    compat_logger.setLevel(logging.INFO)
    compat_logger.propagate = False
    compat_logger.addHandler(compat_handler)
    sys.modules["scrapy"] = scrapy
    try:
        log_scrapy_info(Settings({"LOG_VERSIONS": ["Scrapy", "Python"]}))
        assert compat_handler.records[0].name == "scrapy.utils.log"
        assert compat_handler.records[0].getMessage() == "Scrapy 2.19.0 started (bot: scrapybot)"
        assert "'Scrapy': '2.19.0'" in compat_handler.records[1].getMessage()
    finally:
        if previous is None:
            sys.modules.pop("scrapy", None)
        else:
            sys.modules["scrapy"] = previous
        compat_logger.removeHandler(compat_handler)
        compat_logger.setLevel(old_compat_level)
        compat_logger.propagate = old_compat_propagate

    class Stats:
        counts: dict[str, int] = {}

        def inc_value(self, key: str) -> None:
            self.counts[key] = self.counts.get(key, 0) + 1

    LogCounterHandler(type("StatsCrawler", (), {"stats": Stats()})()).emit(record)
    assert Stats.counts == {"log_count/WARNING": 1}

    null_handler = _get_handler(Settings({"LOG_ENABLED": False}))
    assert isinstance(null_handler, logging.NullHandler)
    assert null_handler.level == logging.DEBUG
    null_handler.close()
    stream_handler = _get_handler(Settings({"LOG_COLOR": False, "LOG_LEVEL": "WARNING"}))
    assert isinstance(stream_handler, logging.StreamHandler)
    assert stream_handler.level == logging.WARNING
    assert isinstance(stream_handler.formatter, logging.Formatter)
    stream_handler.close()
    log_path = Path(".spideroxide-log-mail-test.log")
    try:
        file_handler = _get_handler(
            Settings({"LOG_FILE": str(log_path), "LOG_FILE_APPEND": False, "LOG_ENCODING": "utf-8"})
        )
        try:
            file_handler.emit(
                logging.LogRecord("scrapy.test", logging.ERROR, __file__, 1, "café", (), None)
            )
        finally:
            file_handler.close()
        assert "café" in log_path.read_text(encoding="utf-8")
    finally:
        log_path.unlink(missing_ok=True)

    root = logging.root
    old_root_level = root.level
    old_handler = get_scrapy_root_handler()
    try:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            configure_logging({"LOG_INSTALL_ROOT_HANDLER": False}, install_root_handler=False)
        assert len(caught) == 1 and caught[0].category is ScrapyDeprecationWarning
        assert get_scrapy_root_handler() is old_handler
        install_scrapy_root_handler(Settings({"LOG_ENABLED": False, "LOG_SHORT_NAMES": True}))
        installed = get_scrapy_root_handler()
        assert isinstance(installed, logging.NullHandler) and installed in root.handlers
        assert len(installed.filters) == 1
        _uninstall_scrapy_root_handler()
        assert get_scrapy_root_handler() is None and installed not in root.handlers
    finally:
        _uninstall_scrapy_root_handler()
        root.setLevel(old_root_level)


async def verify_logs() -> None:
    engine_logger = logging.getLogger("spideroxide.engine")
    handler = CaptureHandler()
    old_level, old_propagate = engine_logger.level, engine_logger.propagate
    engine_logger.addHandler(handler)
    engine_logger.setLevel(logging.DEBUG)
    engine_logger.propagate = False
    try:
        for engine in ("python", "rust"):
            handler.records.clear()
            crawler = Crawler(
                FixtureSpider,
                {
                    "ENGINE_BACKEND": engine,
                    "ITEM_PIPELINES": [FixturePipeline],
                    "LOG_FORMATTER": "__main__.FixtureFormatter",
                    "RETRY_ENABLED": False,
                },
                downloader=FixtureDownloader(),
            )
            result = await crawler.crawl()
            assert result.items == ({"kind": "scraped"},), (engine, result.items)
            messages = [(record.levelno, record.getMessage()) for record in handler.records]
            assert any(
                level == logging.DEBUG and message.startswith("Crawled (200)")
                for level, message in messages
            ), (engine, messages)
            assert (logging.INFO, "formatted scraped") in messages, (engine, messages)
            assert any(
                level == logging.INFO and message.startswith("Dropped: fixture dropped")
                for level, message in messages
            ), (engine, messages)
            assert any(
                level == logging.ERROR and message.startswith("Error processing")
                for level, message in messages
            ), (engine, messages)
            assert any(
                level == logging.ERROR and message.startswith("Error downloading")
                for level, message in messages
            ), (engine, messages)
            assert any(
                record.exc_info
                for record in handler.records
                if record.msg == "Error processing %(item)s"
            )
            assert all(record.spider is crawler.spider for record in handler.records)

        handler.records.clear()
        quiet = Crawler(
            FixtureSpider,
            {
                "LOG_FORMATTER": QuietFormatter,
                "ITEM_PIPELINES": [FixturePipeline],
                "RETRY_ENABLED": False,
            },
            downloader=FixtureDownloader(),
        )
        await quiet.crawl()
        assert not any(record.msg.startswith("Crawled") for record in handler.records)
    finally:
        engine_logger.removeHandler(handler)
        engine_logger.setLevel(old_level)
        engine_logger.propagate = old_propagate


async def verify_mail() -> None:
    assert _to_bytes_or_none(None) is None
    assert _to_bytes_or_none("café") == "café".encode()
    assert _to_bytes_or_none(b"raw") == b"raw"
    debug_logger = logging.getLogger("spideroxide.mail")
    debug_handler = CaptureHandler()
    old_debug_level, old_debug_propagate = debug_logger.level, debug_logger.propagate
    debug_logger.addHandler(debug_handler)
    debug_logger.setLevel(logging.DEBUG)
    debug_logger.propagate = False
    try:
        callbacks: list[dict[str, object]] = []
        debug_sender = MailSender(debug=True, smtpuser="café", smtppass="secret")
        assert debug_sender.smtpuser == b"caf\xc3\xa9"
        assert (
            debug_sender.send(
                ["to@example.test"],
                "attachments",
                "café",
                cc="cc@example.test",
                attachs=[("file.bin", "application/octet-stream", BytesIO(b"\xff\x00"))],
                charset="utf-8",
                _callback=lambda **kwargs: callbacks.append(kwargs),
            )
            is None
        )
        callback = callbacks[0]
        assert callback["to"] == ["to@example.test"] and callback["cc"] == ["cc@example.test"]
        msg = callback["msg"]
        assert msg["Cc"] == "cc@example.test"
        assert msg["To"] == "to@example.test"
        assert msg.is_multipart() and msg.get_payload()[1].get_payload(decode=True) == b"\xff\x00"
        assert msg.get_payload()[1]["Content-Disposition"] == 'attachment; filename="file.bin"'
        assert debug_handler.records[0].levelno == logging.DEBUG
        assert "Attachs=1" in debug_handler.records[0].getMessage()
        debug_sender.send(
            "to@example.test", "plain", "hello", _callback=lambda **kwargs: callbacks.append(kwargs)
        )
        plain_msg = callbacks[-1]["msg"]
        assert not plain_msg.is_multipart()
        assert message_from_string(plain_msg.as_string())["Subject"] == "plain"
    finally:
        debug_logger.removeHandler(debug_handler)
        debug_logger.setLevel(old_debug_level)
        debug_logger.propagate = old_debug_propagate

    recipients: list[str] = []
    payloads: list[bytes] = []
    errors: list[Exception] = []

    async def smtp(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.write(b"220 localhost ESMTP\r\n")
        await writer.drain()
        try:
            while line := await reader.readline():
                command = line.decode("ascii").strip().lower()
                if command.startswith("ehlo "):
                    writer.write(b"250 localhost\r\n")
                elif command.startswith(("mail from:", "rcpt to:")):
                    if command.startswith("rcpt to:"):
                        recipients.append(command)
                    writer.write(b"250 OK\r\n")
                elif command == "data":
                    writer.write(b"354 End data with <CR><LF>.<CR><LF>\r\n")
                    await writer.drain()
                    data = bytearray()
                    while line := await reader.readline():
                        if line == b".\r\n":
                            break
                        data.extend(line)
                    payloads.append(bytes(data))
                    writer.write(b"250 queued\r\n")
                elif command == "quit":
                    writer.write(b"221 bye\r\n")
                    await writer.drain()
                    break
                else:
                    raise AssertionError(f"unexpected SMTP command: {command}")
                await writer.drain()
        except Exception as error:
            errors.append(error)
            raise
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(smtp, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    success_logger = logging.getLogger("spideroxide.mail")
    success_handler = CaptureHandler()
    old_success_level, old_success_propagate = success_logger.level, success_logger.propagate
    success_logger.addHandler(success_handler)
    success_logger.setLevel(logging.INFO)
    success_logger.propagate = False
    try:
        sender = MailSender(smtphost="127.0.0.1", smtpport=port)
        delivery = sender.send("to@example.test", "hello", "the body", cc=["cc@example.test"])
        assert delivery is not None
        await asyncio.wait_for(delivery, timeout=5)
        assert len(recipients) == 2
        assert b"Subject: hello" in payloads[0]
        assert b"the body" in payloads[0]
        assert not errors, errors
        assert len(success_handler.records) == 1
        assert success_handler.records[0].levelno == logging.INFO
        assert success_handler.records[0].args == {
            "mailto": ["to@example.test"],
            "mailcc": ["cc@example.test"],
            "mailsubject": "hello",
            "mailattachs": 0,
        }
    finally:
        success_logger.removeHandler(success_handler)
        success_logger.setLevel(old_success_level)
        success_logger.propagate = old_success_propagate
        server.close()
        await server.wait_closed()
    settings = Settings({"MAIL_HOST": "127.0.0.1", "MAIL_PORT": port})
    configured = MailSender.from_crawler(type("ConfiguredCrawler", (), {"settings": settings})())
    assert configured.smtphost == "127.0.0.1" and configured.smtpport == port
    mail_logger = logging.getLogger("spideroxide.mail")
    handler = CaptureHandler()
    old_propagate = mail_logger.propagate
    mail_logger.addHandler(handler)
    mail_logger.propagate = False
    try:
        failure = configured.send("to@example.test", "subject", "body")
        assert failure is not None
        try:
            await asyncio.wait_for(failure, timeout=5)
        except OSError:
            pass
        else:
            raise AssertionError("mail delivery to a closed SMTP port succeeded")
        assert len(handler.records) == 1 and handler.records[0].levelno == logging.ERROR
    finally:
        mail_logger.removeHandler(handler)
        mail_logger.propagate = old_propagate


async def main() -> None:
    verify_formatter_api()
    await verify_logs()
    await verify_mail()
    verify_logging_utils()


if __name__ == "__main__":
    asyncio.run(main())
    print("Logging and mail passed: both engines, formatter overrides, drop levels, and local SMTP")
