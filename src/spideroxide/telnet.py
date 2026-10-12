from __future__ import annotations

from .exceptions import NotConfigured

update_telnet_vars = object()


class TelnetConsole:
    """Scrapy import surface; Twisted's authenticated manhole needs reactor support."""

    def __init__(self, crawler: object) -> None:
        settings = crawler.settings  # type: ignore[attr-defined]
        if not settings.getbool("TELNETCONSOLE_ENABLED"):
            raise NotConfigured
        raise NotConfigured(
            "TelnetConsole requires the Twisted reactor and Conch manhole (issue #50); "
            "use REMOTE_CONTROL_ENABLED for asyncio-native authenticated HTTP control."
        )

    @classmethod
    def from_crawler(cls, crawler: object) -> TelnetConsole:
        return cls(crawler)

    def start_listening(self) -> None:
        raise NotImplementedError("TelnetConsole requires Twisted reactor support (issue #50)")

    def stop_listening(self) -> None:
        raise NotImplementedError("TelnetConsole requires Twisted reactor support (issue #50)")

    def protocol(self) -> object:
        raise NotImplementedError("TelnetConsole requires Twisted Conch (issue #50)")

    def _get_telnet_vars(self) -> dict[str, object]:
        raise NotImplementedError("TelnetConsole requires Twisted Conch (issue #50)")
