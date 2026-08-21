from __future__ import annotations

import codecs
import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from typing import Any
from urllib.parse import urljoin, urlsplit

from w3lib.encoding import html_to_unicode, read_bom
from w3lib.html import get_base_url, strip_html5_whitespace

from .headers import Headers
from .selectors import Selector, SelectorList

Callback = Callable[["Response"], object]
Errback = Callable[[BaseException], object]

_XML_DECLARATION_ENCODING = re.compile(
    rb"""^\s*<\?xml[^>]*\bencoding\s*=\s*["']\s*([A-Za-z0-9._:-]+)""",
    re.IGNORECASE,
)


def _validate_url(url: str) -> None:
    parts = urlsplit(url)
    if parts.scheme.lower() not in {"http", "https"} or parts.hostname is None:
        raise ValueError(f"request URL must be an absolute HTTP or HTTPS URL: {url!r}")


def _normalized_encoding(name: str) -> str:
    normalized = codecs.lookup(name).name
    if normalized in {"utf-16", "utf-32"}:
        return f"{normalized}-be"
    return normalized


def _content_type_encoding(content_type: str | None) -> str | None:
    if content_type is None:
        return None
    for parameter in content_type.split(";")[1:]:
        name, separator, value = parameter.strip().partition("=")
        if separator and name.lower() == "charset":
            return _normalized_encoding(value.strip("\"'"))
    return None


def _xml_body_encoding(body: bytes) -> str | None:
    bom_encoding, _ = read_bom(body)
    if bom_encoding is not None:
        return bom_encoding
    for prefix, encoding in (
        (b"\x00\x00\x00<", "utf-32-be"),
        (b"<\x00\x00\x00", "utf-32-le"),
        (b"\x00<\x00?", "utf-16-be"),
        (b"<\x00?\x00", "utf-16-le"),
    ):
        if body.startswith(prefix):
            return encoding
    match = _XML_DECLARATION_ENCODING.search(body[:4096])
    if match is not None:
        return _normalized_encoding(match.group(1).decode("ascii"))
    return None


def _url_from_selector(selector: Selector) -> str:
    if isinstance(selector.root, str):
        return strip_html5_whitespace(selector.root)
    if not hasattr(selector.root, "tag"):
        raise ValueError(f"unsupported selector: {selector}")
    if selector.root.tag not in {"a", "link"}:
        raise ValueError(
            f"only <a> and <link> element selectors are supported, got <{selector.root.tag}>"
        )
    href = selector.root.get("href")
    if href is None:
        raise ValueError(f"<{selector.root.tag}> element has no href attribute")
    return strip_html5_whitespace(href)


@dataclass(frozen=True, slots=True)
class Request:
    url: str
    callback: Callback | None = None
    method: str = "GET"
    headers: Headers = field(default_factory=Headers)
    body: bytes = b""
    cookies: Mapping[str, str] = field(default_factory=dict)
    meta: dict[str, Any] = field(default_factory=dict)
    encoding: str = "utf-8"
    priority: int = 0
    dont_filter: bool = False
    errback: Errback | None = None
    flags: tuple[str, ...] = ()
    cb_kwargs: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _validate_url(self.url)
        object.__setattr__(self, "method", self.method.upper())
        object.__setattr__(self, "body", bytes(self.body))
        object.__setattr__(self, "headers", self.headers.copy())
        object.__setattr__(self, "cookies", dict(self.cookies))
        object.__setattr__(self, "meta", dict(self.meta))
        object.__setattr__(self, "cb_kwargs", dict(self.cb_kwargs))
        object.__setattr__(self, "flags", tuple(self.flags))

    def replace(self, **changes: object) -> Request:
        values: dict[str, object] = {
            "headers": self.headers.copy(),
            "cookies": dict(self.cookies),
            "meta": dict(self.meta),
            "cb_kwargs": dict(self.cb_kwargs),
        }
        values.update(changes)
        return replace(self, **values)

    def copy(self) -> Request:
        return self.replace()

    def follow(
        self,
        url: str,
        *,
        callback: Callback | None = None,
        method: str = "GET",
        headers: Headers | None = None,
        body: bytes = b"",
        cookies: Mapping[str, str] | None = None,
        meta: Mapping[str, Any] | None = None,
        priority: int | None = None,
        dont_filter: bool = False,
        errback: Errback | None = None,
        cb_kwargs: Mapping[str, Any] | None = None,
    ) -> Request:
        return Request(
            url=urljoin(self.url, url),
            callback=callback,
            method=method,
            headers=headers or Headers(),
            body=body,
            cookies=cookies or {},
            meta=dict(meta or {}),
            priority=self.priority if priority is None else priority,
            dont_filter=dont_filter,
            errback=errback,
            cb_kwargs=dict(cb_kwargs or {}),
        )

    def __repr__(self) -> str:
        return f"<{self.method} {self.url}>"


@dataclass(frozen=True, slots=True)
class Response:
    url: str
    status: int = 200
    headers: Headers = field(default_factory=Headers)
    body: bytes = b""
    request: Request | None = None
    flags: tuple[str, ...] = ()
    protocol: str | None = None

    def __post_init__(self) -> None:
        _validate_url(self.url)
        if not 100 <= self.status <= 599:
            raise ValueError("response status must be between 100 and 599")
        object.__setattr__(self, "headers", self.headers.copy())
        object.__setattr__(self, "body", bytes(self.body))
        object.__setattr__(self, "flags", tuple(self.flags))

    @property
    def meta(self) -> dict[str, Any]:
        if self.request is None:
            raise AttributeError("response has no request")
        return self.request.meta

    def replace(self, **changes: object) -> Response:
        values: dict[str, object] = {"headers": self.headers.copy()}
        values.update(changes)
        return replace(self, **values)

    def urljoin(self, url: str) -> str:
        return urljoin(self.url, url)

    def follow(self, url: str, **kwargs: object) -> Request:
        base = self.request or Request(self.url)
        return base.follow(self.urljoin(url), **kwargs)

    def __repr__(self) -> str:
        return f"<{self.status} {self.url}>"


@dataclass(frozen=True, slots=True)
class TextResponse(Response):
    encoding: str | None = None
    _cached_text: str | None = field(default=None, init=False, repr=False, compare=False)
    _cached_selector: Selector | None = field(default=None, init=False, repr=False, compare=False)
    _cached_base_url: str | None = field(default=None, init=False, repr=False, compare=False)
    _decode_content_type: str | None = field(default=None, init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        Response.__post_init__(self)
        content_type = self.headers.get("Content-Type")
        content_type_text = content_type.decode("latin-1") if content_type is not None else None
        if self.encoding is not None:
            content_type_text = f"text/html; charset={self.encoding}"
        selector_type = self._selector_type()
        if selector_type == "html":
            detected = html_to_unicode(
                content_type_text,
                self.body[:4096],
                default_encoding="utf-8",
            )[0]
        else:
            detected = (
                (read_bom(self.body)[0] if self.encoding is None else None)
                or _content_type_encoding(content_type_text)
                or (_xml_body_encoding(self.body) if selector_type == "xml" else None)
                or "utf-8"
            )
        object.__setattr__(self, "encoding", detected)
        object.__setattr__(self, "_decode_content_type", content_type_text)

    @property
    def text(self) -> str:
        if self._cached_text is None:
            assert self.encoding is not None
            if self._selector_type() == "html":
                text = html_to_unicode(
                    self._decode_content_type,
                    self.body,
                    default_encoding="utf-8",
                )[1]
            else:
                bom_encoding, bom = read_bom(self.body)
                body = (
                    self.body[len(bom) :]
                    if bom_encoding is not None and bom is not None
                    else self.body
                )
                text = body.decode(self.encoding, errors="replace")
            object.__setattr__(self, "_cached_text", text)
        assert self._cached_text is not None
        return self._cached_text

    def json(self, **kwargs: object) -> object:
        return json.loads(self.body, **kwargs)

    def _selector_type(self) -> str:
        content_type = self.headers.get("Content-Type", b"").decode("latin-1").lower()
        media_type = content_type.partition(";")[0].strip()
        if media_type == "application/json" or media_type.endswith("+json"):
            return "json"
        if media_type == "application/xhtml+xml":
            return "html"
        if media_type in {"application/xml", "text/xml"} or media_type.endswith("+xml"):
            return "xml"
        return "html"

    def _selector_base_url(self, selector_type: str) -> str:
        if selector_type != "html":
            return self.url
        if self._cached_base_url is None:
            assert self.encoding is not None
            object.__setattr__(
                self,
                "_cached_base_url",
                get_base_url(self.text[:4096], self.url, self.encoding),
            )
        assert self._cached_base_url is not None
        return self._cached_base_url

    def urljoin(self, url: str) -> str:
        return urljoin(self._selector_base_url(self._selector_type()), url)

    def follow(self, url: str | Selector, **kwargs: object) -> Request:
        if isinstance(url, Selector):
            url = _url_from_selector(url)
        elif isinstance(url, SelectorList):
            raise ValueError("SelectorList is not supported")
        return Response.follow(self, url, **kwargs)

    @property
    def selector(self) -> Selector:
        if self._cached_selector is None:
            selector_type = self._selector_type()
            assert self.encoding is not None
            object.__setattr__(
                self,
                "_cached_selector",
                (
                    Selector(
                        body=self.body,
                        type=selector_type,
                        base_url=self._selector_base_url(selector_type),
                    )
                    if selector_type == "json"
                    else Selector(
                        text=self.text,
                        type=selector_type,
                        base_url=self._selector_base_url(selector_type),
                    )
                ),
            )
        assert self._cached_selector is not None
        return self._cached_selector

    def css(self, query: str) -> SelectorList:
        return self.selector.css(query)

    def xpath(
        self,
        query: str,
        namespaces: Mapping[str, str] | None = None,
        **kwargs: object,
    ) -> SelectorList:
        return self.selector.xpath(query, namespaces=namespaces, **kwargs)

    def jmespath(self, query: str, **kwargs: object) -> SelectorList:
        return self.selector.jmespath(query, **kwargs)
