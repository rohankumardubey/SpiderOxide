from __future__ import annotations

import logging
import os
import re
import warnings
from collections.abc import MutableMapping
from typing import Any, TypedDict

from .exceptions import ScrapyDeprecationWarning
from .http import Request, Response

SCRAPEDMSG = "Scraped from %(src)s" + os.linesep + "%(item)s"
DROPPEDMSG = "Dropped: %(exception)s" + os.linesep + "%(item)s"
CRAWLEDMSG = (
    "Crawled (%(status)s) %(request)s%(request_flags)s (referer: %(referer)s)%(response_flags)s"
)
ITEMERRORMSG = "Error processing %(item)s"
SPIDERERRORMSG = "Spider error processing %(request)s (referer: %(referer)s)"
DOWNLOADERRORMSG_SHORT = "Error downloading %(request)s"
DOWNLOADERRORMSG_LONG = "Error downloading %(request)s: %(errmsg)s"
_MAPPING_PLACEHOLDER = re.compile(r"%\(\w+\)")


class LogFormatterResult(TypedDict):
    level: int
    msg: str
    args: dict[str, Any] | tuple[Any, ...]


def _referer(request: Request) -> str | None:
    value = request.headers.get("Referer")
    return None if value is None else value.decode("latin-1")


class LogFormatter:
    def crawled(self, request: Request, response: Response, spider: object) -> LogFormatterResult:
        request_flags = f" {list(request.flags)!s}" if request.flags else ""
        response_flags = f" {list(response.flags)!s}" if response.flags else ""
        return {
            "level": logging.DEBUG,
            "msg": CRAWLEDMSG,
            "args": {
                "status": response.status,
                "request": request,
                "request_flags": request_flags,
                "referer": _referer(request),
                "response_flags": response_flags,
                "flags": response_flags,
            },
        }

    def scraped(
        self, item: Any, response: Response | BaseException | None, spider: object
    ) -> LogFormatterResult:
        if response is None:
            cls = type(spider)
            src: object = f"{cls.__module__}.{cls.__qualname__}.start"
        elif callable(getattr(response, "getErrorMessage", None)):
            src = response.getErrorMessage()
        else:
            src = response
        return {
            "level": logging.DEBUG,
            "msg": SCRAPEDMSG,
            "args": {"src": src, "item": item},
        }

    def dropped(
        self, item: Any, exception: BaseException, response: object, spider: object
    ) -> LogFormatterResult:
        level = getattr(exception, "log_level", None)
        if level is None:
            level = spider.crawler.settings["DEFAULT_DROPITEM_LOG_LEVEL"]
        if isinstance(level, str):
            level = getattr(logging, level)
        return {
            "level": level,
            "msg": DROPPEDMSG,
            "args": {"exception": exception, "item": item},
        }

    def item_error(
        self, item: Any, exception: BaseException, response: object, spider: object
    ) -> LogFormatterResult:
        return {"level": logging.ERROR, "msg": ITEMERRORMSG, "args": {"item": item}}

    def spider_error(
        self, failure: object, request: Request, response: object, spider: object
    ) -> LogFormatterResult:
        return {
            "level": logging.ERROR,
            "msg": SPIDERERRORMSG,
            "args": {"request": request, "referer": _referer(request)},
        }

    def download_error(
        self, failure: object, request: Request, spider: object, errmsg: str | None = None
    ) -> LogFormatterResult:
        args: dict[str, Any] = {"request": request}
        if errmsg:
            args["errmsg"] = errmsg
        return {
            "level": logging.ERROR,
            "msg": DOWNLOADERRORMSG_LONG if errmsg else DOWNLOADERRORMSG_SHORT,
            "args": args,
        }

    @classmethod
    def from_crawler(cls, crawler: object) -> LogFormatter:
        return cls()


def logformatter_adapter(logkws: MutableMapping[str, Any]) -> tuple[Any, ...]:
    level = logkws.get("level", logging.INFO)
    message = logkws.get("msg") or ""
    args = logkws.get("args")
    if not args:
        if _MAPPING_PLACEHOLDER.search(message):
            warnings.warn(
                f"A log formatter method returned msg {message!r} with "
                f"%(name)s placeholders and no args. Interpolating msg with "
                f"the returned dict is deprecated, return those values under "
                f"args instead.",
                ScrapyDeprecationWarning,
                stacklevel=1,
            )
            return level, message, logkws
        return level, message
    if isinstance(args, tuple):
        return level, message, *args
    return level, message, args
