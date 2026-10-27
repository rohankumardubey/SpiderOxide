"""Opt-in loopback Twisted Conch console."""

from __future__ import annotations

import hmac
import logging
import os
import pprint
import secrets
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

from . import signals
from .exceptions import NotConfigured
from .remote_control import _job_files_dir, _write_job_file
from .trackref import print_live_refs
from .twisted_compat import ASYNCIO_REACTOR, is_reactor_installed, verify_installed_reactor
from .utils import maybe_await

if TYPE_CHECKING:
    from twisted.conch.telnet import TelnetTransport
    from twisted.internet.interfaces import IListeningPort

    from .crawler import Crawler

logger = logging.getLogger(__name__)
update_telnet_vars = object()


class TelnetConsole:
    def __init__(self, crawler: Crawler) -> None:
        settings = crawler.settings
        if not settings.getbool("TELNETCONSOLE_ENABLED"):
            raise NotConfigured
        if settings.get("TWISTED_REACTOR") != ASYNCIO_REACTOR:
            raise RuntimeError("TelnetConsole requires the opt-in Twisted reactor (asyncio)")
        if not is_reactor_installed():
            raise RuntimeError("TelnetConsole requires an installed Twisted reactor")
        verify_installed_reactor(ASYNCIO_REACTOR)
        try:
            from twisted.conch import manhole, telnet
            from twisted.conch.insults import insults
            from twisted.internet import protocol
        except ImportError as error:
            raise RuntimeError("TelnetConsole requires spideroxide[twisted] with Conch") from error

        self._insults = insults
        self._manhole = manhole
        self._telnet = telnet
        self._protocol = protocol
        self.crawler = crawler
        self.noisy = False
        self.port: IListeningPort | None = None
        self._clients: set[TelnetTransport] = set()
        host = settings["TELNETCONSOLE_HOST"]
        if not isinstance(host, str) or host not in ("127.0.0.1", "::1"):
            raise ValueError("TelnetConsole only listens on a numeric loopback address")
        self.host = host
        try:
            self.portrange = [int(port) for port in settings.getlist("TELNETCONSOLE_PORT")]
        except (TypeError, ValueError) as error:
            raise ValueError("Invalid TELNETCONSOLE_PORT") from error
        if (
            len(self.portrange) > 2
            or any(port < 0 or port > 65535 for port in self.portrange)
            or (len(self.portrange) == 2 and self.portrange[0] > self.portrange[1])
        ):
            raise ValueError("Invalid TELNETCONSOLE_PORT")
        username = settings["TELNETCONSOLE_USERNAME"]
        password = settings["TELNETCONSOLE_PASSWORD"]
        if not isinstance(username, str) or not username:
            raise ValueError("TELNETCONSOLE_USERNAME must be nonempty")
        if password is not None and (not isinstance(password, str) or len(password) < 16):
            raise ValueError("TELNETCONSOLE_PASSWORD must have at least 16 characters")
        self.username = username
        self._generated_password = password is None
        if self._generated_password:
            if os.name != "posix":
                raise RuntimeError("Generated Telnet credentials require POSIX private files")
        self.password = secrets.token_urlsafe(32) if password is None else password
        self._jobs_dir = _job_files_dir(settings.get("TELNETCONSOLE_JOBS_DIR"))
        self._job_file_path: Path | None = None
        crawler.signals.connect(self.start_listening, signals.engine_started)
        crawler.signals.connect(self.stop_listening, signals.engine_stopped)

    @classmethod
    def from_crawler(cls, crawler: Crawler) -> TelnetConsole:
        return cls(crawler)

    async def start_listening(self) -> None:
        if self.port is not None:
            return
        from twisted.internet import reactor
        from twisted.internet.error import CannotListenError

        factory = self._protocol.ServerFactory()
        factory.protocol = self.protocol
        ports = [0] if not self.portrange else range(self.portrange[0], self.portrange[-1] + 1)
        try:
            for number in ports:
                try:
                    self.port = reactor.listenTCP(number, factory, interface=self.host)
                    break
                except CannotListenError:
                    if number == ports[-1]:
                        raise
            if self.port is None:
                raise RuntimeError("TelnetConsole could not bind a port")
            address = self.port.getHost()
            if self._generated_password:
                spider = self.crawler.spider
                if spider is None:
                    raise RuntimeError("TelnetConsole requires a running spider")
                path = self._jobs_dir / f"telnet-{os.getpid()}-{uuid.uuid4().hex}.json"
                _write_job_file(
                    path,
                    {
                        "version": 1,
                        "pid": os.getpid(),
                        "host": self.host,
                        "port": address.port,
                        "username": self.username,
                        "password": self.password,
                        "spider": spider.name,
                    },
                )
                self._job_file_path = path
            logger.info("Telnet console listening on %s:%d", address.host, address.port)
        except BaseException:
            await self.stop_listening()
            raise

    async def stop_listening(self) -> None:
        port, self.port = self.port, None
        try:
            for client in tuple(self._clients):
                client.transport.loseConnection()
            if port is not None:
                await maybe_await(port.stopListening())
        finally:
            if self._job_file_path is not None:
                self._job_file_path.unlink(missing_ok=True)
                self._job_file_path = None

    def protocol(self) -> object:
        from twisted.cred.credentials import UsernamePassword
        from twisted.cred.error import UnauthorizedLogin
        from twisted.internet.defer import fail, succeed

        console = self

        class ConsoleTransport(self._telnet.TelnetTransport):
            def connectionMade(self) -> None:
                console._clients.add(self)
                super().connectionMade()

            def connectionLost(self, reason: object) -> None:
                try:
                    super().connectionLost(reason)
                finally:
                    console._clients.discard(self)

        class Portal:
            def login(self, credentials: object, mind: object, *interfaces: object) -> object:
                if (
                    not isinstance(credentials, UsernamePassword)
                    or not hmac.compare_digest(credentials.username, console.username.encode())
                    or not hmac.compare_digest(credentials.password, console.password.encode())
                ):
                    return fail(UnauthorizedLogin("Invalid credentials"))
                session = console._telnet.TelnetBootstrapProtocol(
                    console._insults.ServerProtocol,
                    console._manhole.Manhole,
                    console._get_telnet_vars(),
                )
                return succeed((interfaces[0], session, lambda: None))

        return ConsoleTransport(self._telnet.AuthenticatingTelnetProtocol, Portal())

    def _get_telnet_vars(self) -> dict[str, object]:
        engine = self.crawler.engine
        variables: dict[str, object] = {
            "engine": engine,
            "spider": self.crawler.spider,
            "crawler": self.crawler,
            "extensions": self.crawler.extensions,
            "stats": self.crawler.stats,
            "settings": self.crawler.settings,
            "est": lambda: pprint.pprint(self.crawler.stats.get_stats()),
            "p": pprint.pprint,
            "prefs": print_live_refs,
            "help": "SpiderOxide Telnet console (local, authenticated, plaintext)",
        }
        self.crawler.signals.send_sync(update_telnet_vars, telnet_vars=variables)
        return variables
