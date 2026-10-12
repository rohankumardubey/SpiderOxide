from __future__ import annotations

import logging
import platform
import pprint
import re
import sys
import warnings
from collections.abc import MutableMapping
from importlib.metadata import version
from logging.config import dictConfig
from typing import Any

import lxml.etree

from .exceptions import ScrapyDeprecationWarning
from .settings import Settings

logger = logging.getLogger(__name__)
_DEFAULT_LOG_VERSIONS = [
    "lxml",
    "libxml2",
    "cssselect",
    "parsel",
    "w3lib",
    "Python",
    "Platform",
]

DEFAULT_LOGGING: dict[str, object] = {
    "version": 1,
    "disable_existing_loggers": False,
    "loggers": {
        "filelock": {"level": "ERROR"},
        "hpack": {"level": "ERROR"},
        "httpcore": {"level": "ERROR"},
        "httpx": {"level": "WARNING"},
        "parso": {"level": "ERROR"},
        "scrapy": {"level": "DEBUG"},
        "spideroxide": {"level": "DEBUG"},
        "twisted": {"level": "ERROR"},
    },
}
_scrapy_root_handler: logging.Handler | None = None


class TopLevelFormatter(logging.Filter):
    def __init__(self, loggers: list[str] | None = None) -> None:
        super().__init__()
        self.loggers = loggers or []

    def filter(self, record: logging.LogRecord) -> bool:
        if any(record.name.startswith(name + ".") for name in self.loggers):
            record.name = record.name.split(".", 1)[0]
        return True


class StreamLogger:
    def __init__(self, logger: logging.Logger, log_level: int = logging.INFO) -> None:
        self.logger = logger
        self.log_level = log_level
        self.linebuf = ""

    def write(self, buf: str) -> None:
        for line in buf.rstrip().splitlines():
            self.logger.log(self.log_level, line.rstrip())

    def flush(self) -> None:
        for handler in self.logger.handlers:
            handler.flush()


class LogCounterHandler(logging.Handler):
    def __init__(self, crawler: object, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.crawler = crawler

    def emit(self, record: logging.LogRecord) -> None:
        self.crawler.stats.inc_value(f"log_count/{record.levelname}")


class SpiderLoggerAdapter(logging.LoggerAdapter):
    def process(
        self, msg: str, kwargs: MutableMapping[str, Any]
    ) -> tuple[str, MutableMapping[str, Any]]:
        extra = kwargs.get("extra")
        if isinstance(extra, MutableMapping):
            extra.update(self.extra)
        else:
            kwargs["extra"] = self.extra
        return msg, kwargs


def _uninstall_scrapy_root_handler() -> None:
    global _scrapy_root_handler
    if _scrapy_root_handler is None:
        return
    if _scrapy_root_handler in logging.root.handlers:
        logging.root.removeHandler(_scrapy_root_handler)
    _scrapy_root_handler.close()
    _scrapy_root_handler = None


def _uninstall_root_handler() -> None:
    _uninstall_scrapy_root_handler()


def _tty_supports_color() -> bool:
    if sys.platform != "win32":
        return True
    build = re.match(r"(\d+)\.(\d+)\.(\d+)", platform.version())
    if build is None or tuple(map(int, build.groups())) < (10, 0, 14393):
        return True
    import ctypes

    kernel32 = ctypes.windll.kernel32
    return bool(kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 7))


def _get_formatter(handler: logging.Handler, settings: Settings) -> logging.Formatter:
    fmt = settings.get("LOG_FORMAT")
    datefmt = settings.get("LOG_DATEFORMAT")
    if (
        isinstance(handler, logging.StreamHandler)
        and not isinstance(handler, logging.FileHandler)
        and settings.getbool("LOG_COLOR")
        and handler.stream.isatty()
        and _tty_supports_color()
    ):
        try:
            from colorlog import ColoredFormatter
        except ImportError:
            pass
        else:
            return ColoredFormatter(fmt=f"%(log_color)s{fmt}", datefmt=datefmt)
    return logging.Formatter(fmt=fmt, datefmt=datefmt)


def _get_handler(settings: Settings) -> logging.Handler:
    filename = settings.get("LOG_FILE")
    handler: logging.Handler
    if filename:
        handler = logging.FileHandler(
            filename,
            mode="a" if settings.getbool("LOG_FILE_APPEND") else "w",
            encoding=settings.get("LOG_ENCODING"),
        )
    elif settings.getbool("LOG_ENABLED"):
        handler = logging.StreamHandler()
    else:
        handler = logging.NullHandler()
    handler.setFormatter(_get_formatter(handler, settings))
    handler.setLevel(settings.get("LOG_LEVEL"))
    if settings.getbool("LOG_SHORT_NAMES"):
        handler.addFilter(TopLevelFormatter(["scrapy"]))
    return handler


def install_scrapy_root_handler(settings: Settings) -> None:
    global _scrapy_root_handler
    _uninstall_scrapy_root_handler()
    logging.root.setLevel(logging.NOTSET)
    _scrapy_root_handler = _get_handler(settings)
    logging.root.addHandler(_scrapy_root_handler)


def get_scrapy_root_handler() -> logging.Handler | None:
    return _scrapy_root_handler


def _software_version(item: str) -> str:
    name = item.lower()
    scrapy = sys.modules.get("scrapy")
    if (
        name == "scrapy"
        and scrapy is not None
        and getattr(scrapy, "spideroxide_compat_version", None)
    ):
        return scrapy.__version__
    if name == "libxml2":
        return ".".join(map(str, lxml.etree.LIBXML_VERSION))
    if name == "platform":
        return platform.platform()
    if name == "python":
        return sys.version.replace("\n", "- ")
    if name == "pyopenssl":
        import OpenSSL.SSL
        import OpenSSL.version

        openssl = OpenSSL.SSL.SSLeay_version(OpenSSL.SSL.SSLEAY_VERSION)
        return f"{OpenSSL.version.__version__} ({openssl.decode('ascii', errors='replace')})"
    return version(item)


def log_scrapy_info(settings: Settings) -> None:
    scrapy = sys.modules.get("scrapy")
    if scrapy is not None and getattr(scrapy, "spideroxide_compat_version", None):
        info_logger = logging.getLogger("scrapy.utils.log")
        name = "Scrapy"
        current_version = scrapy.__version__
    else:
        info_logger = logger
        name = "SpiderOxide"
        current_version = version("spideroxide")
    info_logger.info(
        f"{name} %(version)s started (bot: %(bot)s)",
        {"version": current_version, "bot": settings["BOT_NAME"]},
    )
    software = settings.getlist("LOG_VERSIONS", _DEFAULT_LOG_VERSIONS)
    if software:
        versions = pprint.pformat(
            {item: _software_version(item) for item in software}, sort_dicts=False
        )
        info_logger.info(f"Versions:\n{versions}")


def configure_logging(
    settings: Settings | dict[str, Any] | None = None,
    install_root_handler: bool | None = None,
) -> None:
    if isinstance(settings, dict) or settings is None:
        settings = Settings(settings)
    if install_root_handler is None:
        install_root_handler = settings.getbool("LOG_INSTALL_ROOT_HANDLER")
    else:
        warnings.warn(
            "The install_root_handler parameter is deprecated. Set the "
            "LOG_INSTALL_ROOT_HANDLER setting instead.",
            ScrapyDeprecationWarning,
            stacklevel=2,
        )
    if not sys.warnoptions:
        logging.captureWarnings(True)
    dictConfig(DEFAULT_LOGGING)
    if settings.getbool("LOG_STDOUT"):
        sys.stdout = StreamLogger(logging.getLogger("stdout"))
    if install_root_handler:
        install_scrapy_root_handler(settings)
