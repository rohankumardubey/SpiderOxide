from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from spideroxide import Crawler, DropItem, Request, Settings, Spider
from spideroxide.logformatter import LogFormatter
from spideroxide.mail import MailSender


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
    try:
        sender = MailSender(smtphost="127.0.0.1", smtpport=port)
        delivery = sender.send("to@example.test", "hello", "the body", cc=["cc@example.test"])
        assert delivery is not None
        await asyncio.wait_for(delivery, timeout=5)
        assert len(recipients) == 2
        assert b"Subject: hello" in payloads[0]
        assert b"the body" in payloads[0]
        assert not errors, errors
    finally:
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
    await verify_logs()
    await verify_mail()


if __name__ == "__main__":
    asyncio.run(main())
    print("Logging and mail passed: both engines, formatter overrides, drop levels, and local SMTP")
