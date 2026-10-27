from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import stat
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

from spideroxide import Crawler, Spider
from spideroxide.telnet import TelnetConsole, update_telnet_vars
from spideroxide.twisted_compat import ASYNCIO_REACTOR


class WaitingSpider(Spider):
    name = "telnet-console"
    start_urls = ("data:text/plain,ready",)
    ready: asyncio.Event
    release: asyncio.Event

    async def parse(self, response: object) -> None:
        self.ready.set()
        await self.release.wait()


async def finish_negotiation(writer: asyncio.StreamWriter, response: bytes) -> None:
    for option in (34, 31, 3):
        assert b"\xff\xfd" + bytes([option]) in response
        writer.write(b"\xff\xfb" + bytes([option]))
    assert b"\xff\xfb\x01" in response
    writer.write(b"\xff\xfd\x01")
    await writer.drain()
    await asyncio.sleep(0)


def settings(engine: str, **extra: object) -> dict[str, object]:
    return {
        "ENGINE_BACKEND": engine,
        "TWISTED_REACTOR": ASYNCIO_REACTOR,
        "TELNETCONSOLE_ENABLED": True,
        "TELNETCONSOLE_PORT": [0],
        "ROBOTSTXT_OBEY": False,
        **extra,
    }


async def connect(
    port: int, username: str, password: str
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter, bytes]:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    assert (await asyncio.wait_for(reader.readuntil(b"Username: "), 3)).endswith(b"Username: ")
    writer.write(username.encode() + b"\r\n")
    await writer.drain()
    assert (await asyncio.wait_for(reader.readuntil(b"Password: "), 3)).endswith(b"Password: ")
    writer.write(b"\xff\xfd\x01" + password.encode() + b"\r\n")
    await writer.drain()
    assert await asyncio.wait_for(reader.readexactly(3), 3) == b"\xff\xfc\x01"
    writer.write(b"\xff\xfe\x01")
    await writer.drain()
    response = await asyncio.wait_for(reader.readuntil(b">>> "), 3)
    await finish_negotiation(writer, response)
    return reader, writer, response


async def verify(engine: str, base: Path) -> None:
    WaitingSpider.ready = asyncio.Event()
    WaitingSpider.release = asyncio.Event()
    job_dir = base / f"jobs-{engine}"
    crawler = Crawler(
        WaitingSpider,
        settings(engine, TELNETCONSOLE_JOBS_DIR=str(job_dir)),
    )

    def add_variable(telnet_vars: dict[str, object]) -> None:
        telnet_vars["extra"] = 42

    crawler.signals.connect(add_variable, update_telnet_vars)
    crawl = asyncio.create_task(crawler.crawl())
    try:
        with patch.object(logging.getLogger("spideroxide.telnet"), "info") as log_info:
            await asyncio.wait_for(WaitingSpider.ready.wait(), 10)
        console = crawler.get_extension(TelnetConsole)
        assert console is not None and console.port is not None
        assert console.password not in str(log_info.call_args_list)
        assert console.host == "127.0.0.1" and console.noisy is False
        assert {
            "crawler",
            "engine",
            "spider",
            "extensions",
            "stats",
            "settings",
            "est",
            "p",
            "prefs",
            "help",
            "extra",
        } <= console._get_telnet_vars().keys()
        address = console.port.getHost()
        assert address.host == "127.0.0.1"
        assert console._generated_password and console._job_file_path is not None
        path = console._job_file_path
        assert stat.S_IMODE(job_dir.stat().st_mode) == 0o700
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        record = json.loads(path.read_text())
        assert record["port"] == address.port and record["pid"] == os.getpid()
        assert record["password"] == console.password
        assert len(console.password) >= 32

        for username, password in (
            ("scrapy", "not-the-password"),
            ("unknown", console.password),
        ):
            reader, writer = await asyncio.open_connection("127.0.0.1", address.port)
            try:
                await asyncio.wait_for(reader.readuntil(b"Username: "), 3)
                writer.write(username.encode() + b"\r\n")
                await writer.drain()
                await asyncio.wait_for(reader.readuntil(b"Password: "), 3)
                writer.write(b"\xff\xfd\x01" + password.encode() + b"\r\n")
                await writer.drain()
                assert await asyncio.wait_for(reader.readexactly(3), 3) == b"\xff\xfc\x01"
                writer.write(b"\xff\xfe\x01")
                await writer.drain()
                assert b"Authentication failed" in await asyncio.wait_for(
                    reader.readuntil(b"Username: "), 3
                )
            finally:
                writer.close()
                await writer.wait_closed()

        reader, writer, _ = await connect(address.port, "scrapy", console.password)
        try:
            writer.write(b"extra + 1\r\n")
            await writer.drain()
            assert b"43" in await asyncio.wait_for(reader.readuntil(b">>> "), 3)
        finally:
            WaitingSpider.release.set()
            await asyncio.wait_for(crawl, 10)

            async def read_to_eof() -> None:
                while await reader.read(1024):
                    pass

            await asyncio.wait_for(read_to_eof(), 3)
            writer.close()
            await writer.wait_closed()
        assert console.port is None and console._job_file_path is None
        assert not path.exists()
        try:
            await asyncio.open_connection("127.0.0.1", address.port)
        except OSError:
            pass
        else:
            raise AssertionError("Telnet listener survived crawler shutdown")
        await console.stop_listening()
    finally:
        WaitingSpider.release.set()
        if not crawl.done():
            await asyncio.wait_for(crawl, 10)


async def verify_explicit(engine: str) -> None:
    WaitingSpider.ready = asyncio.Event()
    WaitingSpider.release = asyncio.Event()
    crawler = Crawler(
        WaitingSpider,
        settings(engine, TELNETCONSOLE_PASSWORD="a-valid-password-1234"),
    )
    crawl = asyncio.create_task(crawler.crawl())
    try:
        await asyncio.wait_for(WaitingSpider.ready.wait(), 10)
        console = crawler.get_extension(TelnetConsole)
        assert console is not None and console.port is not None
        assert console._job_file_path is None and not console._generated_password
        reader, writer, response = await connect(
            console.port.getHost().port, "scrapy", console.password
        )
        assert b">>> " in response
        writer.close()
        await writer.wait_closed()
    finally:
        WaitingSpider.release.set()
        await asyncio.wait_for(crawl, 10)


async def verify_shared_reactor(engine: str) -> None:
    class FirstSpider(WaitingSpider):
        name = "first-telnet"
        ready = asyncio.Event()
        release = asyncio.Event()

    class SecondSpider(WaitingSpider):
        name = "second-telnet"
        ready = asyncio.Event()
        release = asyncio.Event()

    config = settings(engine, TELNETCONSOLE_PASSWORD="a-valid-password-1234")
    first = Crawler(FirstSpider, config)
    second = Crawler(SecondSpider, config)
    first_task = asyncio.create_task(first.crawl())
    second_task = asyncio.create_task(second.crawl())
    try:
        await asyncio.wait_for(
            asyncio.gather(FirstSpider.ready.wait(), SecondSpider.ready.wait()), 10
        )
        first_console = first.get_extension(TelnetConsole)
        second_console = second.get_extension(TelnetConsole)
        assert first_console is not None and second_console is not None
        assert first_console.port is not None and second_console.port is not None
        reader, writer, _ = await connect(
            first_console.port.getHost().port, "scrapy", first_console.password
        )
        try:
            FirstSpider.release.set()
            await asyncio.wait_for(first_task, 10)

            async def read_to_eof() -> None:
                while await reader.read(1024):
                    pass

            await asyncio.wait_for(read_to_eof(), 3)
            assert not first_console._clients
            _, other_writer, _ = await connect(
                second_console.port.getHost().port, "scrapy", second_console.password
            )
            other_writer.close()
            await other_writer.wait_closed()
        finally:
            writer.close()
            await writer.wait_closed()
    finally:
        FirstSpider.release.set()
        SecondSpider.release.set()
        await asyncio.wait_for(asyncio.gather(first_task, second_task), 10)


async def verify_rejected(engine: str, base: Path) -> None:
    for bad in (
        {"TELNETCONSOLE_HOST": "0.0.0.0"},
        {"TELNETCONSOLE_HOST": "localhost"},
        {"TELNETCONSOLE_PASSWORD": "short"},
        {"TELNETCONSOLE_PORT": [70000]},
        {"TELNETCONSOLE_PORT": [3, 2]},
        {"TELNETCONSOLE_PORT": [1, 2, 3]},
        {"TELNETCONSOLE_USERNAME": ""},
        {"TWISTED_REACTOR": None},
    ):
        try:
            TelnetConsole.from_crawler(Crawler(WaitingSpider, settings(engine, **bad)))
        except (ValueError, RuntimeError):
            pass
        else:
            raise AssertionError(f"Unsafe Telnet settings were accepted: {bad}")

        crawler = Crawler(WaitingSpider, settings(engine, **bad))
        try:
            await crawler.crawl()
        except (ValueError, RuntimeError):
            assert crawler.engine is None
        else:
            raise AssertionError(f"Crawler silently disabled unsafe Telnet settings: {bad}")

    insecure = base / f"insecure-{engine}"
    insecure.mkdir(mode=0o755)
    insecure.chmod(0o755)
    WaitingSpider.ready = asyncio.Event()
    WaitingSpider.release = asyncio.Event()
    crawler = Crawler(WaitingSpider, settings(engine, TELNETCONSOLE_JOBS_DIR=str(insecure)))
    with patch.object(logging.getLogger("spideroxide.signals"), "exception") as logged:
        crawl = asyncio.create_task(crawler.crawl())
        try:
            await asyncio.wait_for(WaitingSpider.ready.wait(), 10)
            console = crawler.get_extension(TelnetConsole)
            assert console is not None and console.port is None
            assert console._job_file_path is None
            logged.assert_called()
        finally:
            WaitingSpider.release.set()
            await asyncio.wait_for(crawl, 10)
    assert not list(insecure.iterdir())
    insecure.rmdir()

    symlink = base / f"symlink-{engine}"
    target = base / f"target-{engine}"
    target.mkdir(mode=0o700)
    symlink.symlink_to(target, target_is_directory=True)
    WaitingSpider.ready = asyncio.Event()
    WaitingSpider.release = asyncio.Event()
    crawler = Crawler(WaitingSpider, settings(engine, TELNETCONSOLE_JOBS_DIR=str(symlink)))
    with patch.object(logging.getLogger("spideroxide.signals"), "exception") as logged:
        crawl = asyncio.create_task(crawler.crawl())
        try:
            await asyncio.wait_for(WaitingSpider.ready.wait(), 10)
            console = crawler.get_extension(TelnetConsole)
            assert console is not None and console.port is None
            logged.assert_called()
        finally:
            WaitingSpider.release.set()
            await asyncio.wait_for(crawl, 10)
    assert not list(target.iterdir())
    symlink.unlink()
    target.rmdir()

    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0))
        reserved.listen()
        number = reserved.getsockname()[1]
        WaitingSpider.ready = asyncio.Event()
        WaitingSpider.release = asyncio.Event()
        crawler = Crawler(
            WaitingSpider,
            settings(
                engine,
                TELNETCONSOLE_PASSWORD="a-valid-password-1234",
                TELNETCONSOLE_PORT=[number],
            ),
        )
        with patch.object(logging.getLogger("spideroxide.signals"), "exception") as logged:
            crawl = asyncio.create_task(crawler.crawl())
            try:
                await asyncio.wait_for(WaitingSpider.ready.wait(), 10)
                console = crawler.get_extension(TelnetConsole)
                assert console is not None and console.port is None
                logged.assert_called()
            finally:
                WaitingSpider.release.set()
                await asyncio.wait_for(crawl, 10)


async def main() -> None:
    assert "twisted.internet.reactor" not in sys.modules
    try:
        TelnetConsole.from_crawler(
            Crawler(
                WaitingSpider,
                settings("python", TELNETCONSOLE_PASSWORD="a-valid-password-1234"),
            )
        )
    except RuntimeError as error:
        assert "installed Twisted reactor" in str(error)
    else:
        raise AssertionError("Console installed a reactor during construction")
    assert "twisted.internet.reactor" not in sys.modules

    with tempfile.TemporaryDirectory() as directory:
        base = Path(directory)
        for backend in ("python", "rust"):
            await verify(backend, base)
            await verify_explicit(backend)
            await verify_shared_reactor(backend)
            await verify_rejected(backend, base)


if __name__ == "__main__":
    asyncio.run(main())
    print(
        "Telnet console passed: authenticated loopback shell, secure defaults, cleanup, and engines"
    )
