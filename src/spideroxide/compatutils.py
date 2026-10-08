from __future__ import annotations

import datetime
import decimal
import json
import re
import warnings
from collections.abc import Iterable
from pathlib import Path
from urllib.parse import ParseResult, urlparse, urlunparse

from itemadapter import ItemAdapter, is_item
from w3lib.url import any_to_uri, parse_url

from .http import Request, Response
from .settings import BaseSettings


class ScrapyJSONEncoder(json.JSONEncoder):
    def default(self, value: object) -> object:
        if isinstance(value, set):
            return list(value)
        if isinstance(value, (datetime.datetime, datetime.date, datetime.time)):
            return value.isoformat()
        if isinstance(value, decimal.Decimal):
            return str(value)
        if isinstance(value, Request):
            return f"<{type(value).__name__} {value.method} {value.url}>"
        if isinstance(value, Response):
            return f"<{type(value).__name__} {value.status} {value.url}>"
        if is_item(value):
            return ItemAdapter(value).asdict()
        return super().default(value)


def job_dir(settings: BaseSettings) -> str | None:
    path = settings["JOBDIR"]
    if not path:
        return None
    directory = Path(str(path))
    directory.mkdir(parents=True, exist_ok=True)
    return str(path)


def url_is_from_spider(url: str | bytes | ParseResult, spider: type[object]) -> bool:
    domains = [spider.name]
    allowed_domains = getattr(spider, "allowed_domains", None)
    if isinstance(allowed_domains, property):
        warnings.warn(
            f"{spider.__name__}.allowed_domains is a property. Properties "
            "cannot be evaluated on a spider class, only on a spider "
            "instance, so it will be ignored here. This affects matching "
            "URLs to spiders, e.g. in the shell, fetch and parse commands. "
            "Define allowed_domains as a plain class attribute instead.",
            UserWarning,
            stacklevel=2,
        )
    elif allowed_domains:
        domains.extend(allowed_domains)
    host = parse_url(url).netloc.lower()
    return bool(host) and any(host == d.lower() or host.endswith(f".{d.lower()}") for d in domains)


def url_has_any_extension(url: str | bytes | ParseResult, extensions: Iterable[str]) -> bool:
    path = parse_url(url).path.lower()
    return any(path.endswith(ext) for ext in extensions)


def add_http_if_no_scheme(url: str) -> str:
    if re.match(r"^\w+://", url, re.IGNORECASE):
        return url
    parts = urlparse(url)
    return ("http:" if parts.netloc else "http://") + url


def guess_scheme(url: str) -> str:
    if (url.startswith("/") and len(url) > 1) or re.match(
        r"^(?:(?:\.(?:\.|[^/.]+)?|~)/.|[a-z]:\\|\\\\)", url, re.IGNORECASE
    ):
        return any_to_uri(url)
    return add_http_if_no_scheme(url)


def strip_url(
    url: str,
    strip_credentials: bool = True,
    strip_default_port: bool = True,
    origin_only: bool = False,
    strip_fragment: bool = True,
) -> str:
    parsed = urlparse(url)
    netloc = parsed.netloc
    if (strip_credentials or origin_only) and (parsed.username or parsed.password):
        netloc = netloc.split("@")[-1]
    if strip_default_port and (parsed.scheme, parsed.port) in {
        ("http", 80),
        ("https", 443),
        ("ftp", 21),
    }:
        netloc = netloc.removesuffix(f":{parsed.port}")
    return urlunparse(
        (
            parsed.scheme,
            netloc,
            "/" if origin_only else parsed.path,
            "" if origin_only else parsed.params,
            "" if origin_only else parsed.query,
            "" if strip_fragment else parsed.fragment,
        )
    )
