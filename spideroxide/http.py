from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from typing import Any
from urllib.parse import urljoin, urlsplit

from .headers import Headers

Callback = Callable[["Response"], object]
Errback = Callable[[BaseException], object]


def _validate_url(url: str) -> None:
    parts = urlsplit(url)
    if parts.scheme.lower() not in {"http", "https"} or parts.hostname is None:
        raise ValueError(f"request URL must be an absolute HTTP or HTTPS URL: {url!r}")


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

    def follow(self, url: str, **kwargs: object) -> Request:
        base = self.request or Request(self.url)
        return base.follow(urljoin(self.url, url), **kwargs)

    def __repr__(self) -> str:
        return f"<{self.status} {self.url}>"


@dataclass(frozen=True, slots=True)
class TextResponse(Response):
    encoding: str | None = None
    _cached_text: str | None = field(default=None, init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        Response.__post_init__(self)
        object.__setattr__(self, "encoding", self.encoding or self._declared_encoding() or "utf-8")

    def _declared_encoding(self) -> str | None:
        content_type = self.headers.get("Content-Type")
        if content_type is None:
            return None
        for parameter in content_type.decode("latin-1").split(";")[1:]:
            name, separator, value = parameter.strip().partition("=")
            if separator and name.lower() == "charset":
                return value.strip("\"'")
        return None

    @property
    def text(self) -> str:
        if self._cached_text is None:
            assert self.encoding is not None
            object.__setattr__(
                self,
                "_cached_text",
                self.body.decode(self.encoding, errors="replace"),
            )
        assert self._cached_text is not None
        return self._cached_text

    def json(self, **kwargs: object) -> object:
        return json.loads(self.text, **kwargs)
