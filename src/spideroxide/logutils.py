from __future__ import annotations

import logging
import sys
import warnings
from collections.abc import MutableMapping
from logging.config import dictConfig
from typing import Any

from .exceptions import ScrapyDeprecationWarning
from .settings import Settings

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
_root_handler: logging.Handler | None = None


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


def _uninstall_root_handler() -> None:
    global _root_handler
    if _root_handler is None:
        return
    if _root_handler in logging.root.handlers:
        logging.root.removeHandler(_root_handler)
    _root_handler.close()
    _root_handler = None


def install_scrapy_root_handler(settings: Settings) -> None:
    global _root_handler
    _uninstall_root_handler()
    logging.root.setLevel(logging.NOTSET)
    filename = settings.get("LOG_FILE")
    if filename:
        _root_handler = logging.FileHandler(
            filename,
            mode="a" if settings.getbool("LOG_FILE_APPEND") else "w",
            encoding=str(settings.get("LOG_ENCODING")),
        )
    elif settings.getbool("LOG_ENABLED"):
        _root_handler = logging.StreamHandler()
    else:
        _root_handler = logging.NullHandler()
    _root_handler.setFormatter(
        logging.Formatter(fmt=settings.get("LOG_FORMAT"), datefmt=settings.get("LOG_DATEFORMAT"))
    )
    _root_handler.setLevel(settings.get("LOG_LEVEL"))
    if settings.getbool("LOG_SHORT_NAMES"):
        _root_handler.addFilter(TopLevelFormatter(["scrapy"]))
    logging.root.addHandler(_root_handler)


def get_scrapy_root_handler() -> logging.Handler | None:
    return _root_handler


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
