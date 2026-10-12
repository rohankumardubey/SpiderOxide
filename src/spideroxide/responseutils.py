from __future__ import annotations

import mimetypes
import os
import re
import tempfile
import webbrowser
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from w3lib.html import get_base_url as html_base_url
from w3lib.html import get_meta_refresh as html_meta_refresh

from .components import load_object
from .downloader import _response_type
from .headers import Headers
from .http import HtmlResponse, Response, TextResponse

_DOCTYPE = re.compile(rb"\s*<!doctype[^<>]*>", re.IGNORECASE)


def get_base_url(response: TextResponse) -> str:
    return html_base_url(response.text[:4096], response.url, response.encoding)


def get_meta_refresh(
    response: TextResponse,
    ignore_tags: Iterable[str] = ("script", "noscript"),
) -> tuple[float | None, str | None]:
    return html_meta_refresh(
        response.text[:4096],
        get_base_url(response),
        response.encoding,
        ignore_tags=ignore_tags,
    )


def response_from_dict(d: Mapping[str, Any]) -> Response:
    values = {**d, "headers": Headers(d.get("headers") or {})}
    class_path = values.get("_class")
    response_class = (
        load_object(class_path)
        if class_path is not None
        else _response_type(values["headers"], url=values["url"], body=values.get("body") or b"")
    )
    if not isinstance(response_class, type) or not issubclass(response_class, Response):
        raise TypeError(f"response class must be a Response subclass, got {response_class!r}")
    return response_class.from_dict(values)


def open_in_browser(
    response: Response,
    _openfunc: Callable[[str], Any] = webbrowser.open,
) -> Any:
    body = response.body
    if isinstance(response, HtmlResponse):
        doctype = _DOCTYPE.match(body)
        position = doctype.end() if doctype else 0
        base_tag = f'<base href="{get_base_url(response)}">'.encode()
        body = body[:position] + base_tag + body[position:]
        suffix = ".html"
    elif isinstance(response, TextResponse):
        suffix = ".txt"
    else:
        content_type = (response.headers.get("Content-Type") or b"").decode("latin-1")
        media_type = content_type.split(";")[0].strip().lower()
        suffix = mimetypes.guess_extension(media_type) or ""
        if not suffix:
            raise TypeError(f"Unsupported response type: {type(response).__name__}")
    descriptor, filename = tempfile.mkstemp(suffix)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(body)
    except BaseException:
        os.unlink(filename)
        raise
    return _openfunc(f"file://{filename}")
