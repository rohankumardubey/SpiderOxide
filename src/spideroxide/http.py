from __future__ import annotations

import base64
import codecs
import copy
import json
import re
import shlex
import warnings
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from ipaddress import IPv4Address, IPv6Address
from typing import Any, ClassVar
from urllib.parse import urlencode, urljoin, urlsplit, urlunsplit

from w3lib.encoding import html_to_unicode, read_bom
from w3lib.html import get_base_url, strip_html5_whitespace
from w3lib.url import safe_url_string

from .headers import HeaderName, Headers, HeaderValues
from .selectors import Selector, SelectorList

Callback = Callable[["Response"], object]
Errback = Callable[[BaseException], object]
HeaderInput = (
    Headers | Mapping[HeaderName, HeaderValues] | Iterable[tuple[HeaderName, HeaderValues]]
)
CookieValue = str | bytes | bool | float | int
CookieInput = Mapping[str | bytes, CookieValue] | Iterable[Mapping[str, CookieValue]]
FormData = Mapping[object, object] | Iterable[tuple[object, object]]

_JSON_UNSET = object()

_XML_DECLARATION_ENCODING = re.compile(
    rb"""^\s*<\?xml[^>]*\bencoding\s*=\s*["']\s*([A-Za-z0-9._:-]+)""",
    re.IGNORECASE,
)


def _prepare_url(url: str, encoding: str) -> str:
    if not isinstance(url, str):
        raise TypeError(f"Request url must be str, got {type(url).__name__}")
    url = safe_url_string(url, encoding=encoding)
    parts = urlsplit(url)
    if parts.scheme.lower() not in {"http", "https"} or parts.hostname is None:
        raise ValueError(f"request URL must be an absolute HTTP or HTTPS URL: {url!r}")
    return url


def _validate_url(url: str) -> None:
    if not isinstance(url, str):
        raise TypeError(f"Response url must be str, got {type(url).__name__}")
    parts = urlsplit(url)
    if parts.scheme.lower() not in {"http", "https"} or parts.hostname is None:
        raise ValueError(f"response URL must be an absolute HTTP or HTTPS URL: {url!r}")


def _body_bytes(body: bytes | str | None, encoding: str, *, text: bool = False) -> bytes:
    if body is None:
        return b""
    if isinstance(body, str):
        if not text:
            return body.encode(encoding)
        return body.encode(encoding)
    if not isinstance(body, bytes):
        raise TypeError(f"request body must be bytes or str, got {type(body).__name__}")
    return body


def _copy_cookies(
    cookies: CookieInput,
) -> dict[str | bytes, CookieValue] | list[dict[str, CookieValue]]:
    if isinstance(cookies, Mapping):
        return dict(cookies)
    if isinstance(cookies, (str, bytes)):
        raise TypeError("cookies must be a mapping or an iterable of mappings")
    copied = []
    for cookie in cookies:
        if not isinstance(cookie, Mapping):
            raise TypeError("verbose cookies must be mappings")
        copied.append(dict(cookie))
    return copied


def _callback_name(spider: object | None, callback: object, field: str) -> str | None:
    if callback is None:
        return None
    name = getattr(callback, "__name__", None)
    function = getattr(callback, "__func__", None)
    owner = getattr(callback, "__self__", None)
    candidate = getattr(spider, name, None) if isinstance(name, str) else None
    if (
        spider is not None
        and owner is spider
        and function is not None
        and getattr(candidate, "__func__", None) is function
    ):
        return name
    raise ValueError(f"{field} {callback!r} is not an instance method of {spider!r}")


def _form_pairs(formdata: FormData, encoding: str) -> list[tuple[bytes, bytes]]:
    def encode(value: object) -> bytes:
        if isinstance(value, bytes):
            return value
        if isinstance(value, str):
            return value.encode(encoding)
        raise TypeError(f"form values must be str or bytes, got {type(value).__name__}")

    items = formdata.items() if isinstance(formdata, Mapping) else formdata
    pairs: list[tuple[bytes, bytes]] = []
    for name, value in items:
        values = (
            value
            if isinstance(value, Iterable) and not isinstance(value, (str, bytes))
            else (value,)
        )
        for current in values:
            pairs.append((encode(name), encode(current)))
    return pairs


def _parse_cookie_header(value: str, cookies: dict[str, str]) -> None:
    for cookie in value.split(";"):
        name, separator, cookie_value = cookie.strip().partition("=")
        if separator:
            cookies[name] = cookie_value


def _curl_request_kwargs(
    command: str,
    *,
    ignore_unknown_options: bool,
) -> dict[str, object]:
    tokens = shlex.split(command)
    if not tokens or tokens[0] != "curl":
        raise ValueError("curl command must start with 'curl'")
    method: str | None = None
    url: str | None = None
    body: str | None = None
    headers: list[tuple[str, str]] = []
    cookies: dict[str, str] = {}
    ignored_with_value = {
        "-A",
        "--user-agent",
        "-e",
        "--referer",
        "-x",
        "--proxy",
        "--connect-timeout",
        "--max-time",
    }
    ignored_flags = {
        "-s",
        "--silent",
        "-S",
        "--show-error",
        "-L",
        "--location",
        "-k",
        "--insecure",
        "--compressed",
        "-i",
        "--include",
    }
    index = 1
    while index < len(tokens):
        token = tokens[index]
        inline_value: str | None = None
        if token.startswith("--") and "=" in token:
            token, inline_value = token.split("=", 1)
        elif len(token) > 2 and token[:2] in {"-X", "-H", "-d", "-b", "-u"}:
            token, inline_value = token[:2], token[2:]
        if token in {
            "-X",
            "--request",
            "-H",
            "--header",
            "-d",
            "--data",
            "--data-raw",
            "--data-ascii",
            "--data-binary",
            "-b",
            "--cookie",
            "-u",
            "--user",
            "--url",
        }:
            if inline_value is None:
                index += 1
                if index >= len(tokens):
                    raise ValueError(f"curl option {token!r} requires a value")
                value = tokens[index]
            else:
                value = inline_value
            if token in {"-X", "--request"}:
                method = value
            elif token in {"-H", "--header"}:
                name, separator, header_value = value.partition(":")
                if not separator:
                    raise ValueError(f"invalid curl header: {value!r}")
                name = name.strip()
                header_value = header_value.lstrip()
                if name.lower() == "cookie":
                    _parse_cookie_header(header_value, cookies)
                else:
                    headers.append((name, header_value))
            elif token in {"-d", "--data", "--data-raw", "--data-ascii", "--data-binary"}:
                body = value if body is None else f"{body}&{value}"
            elif token in {"-b", "--cookie"}:
                _parse_cookie_header(value, cookies)
            elif token in {"-u", "--user"}:
                credentials = base64.b64encode(value.encode()).decode("ascii")
                headers.append(("Authorization", f"Basic {credentials}"))
            else:
                url = value
        elif token in ignored_with_value:
            if inline_value is None:
                index += 1
                if index >= len(tokens):
                    raise ValueError(f"curl option {token!r} requires a value")
        elif token in ignored_flags:
            pass
        elif token.startswith("-"):
            if not ignore_unknown_options:
                raise ValueError(f"unsupported curl option: {token}")
        elif url is None:
            url = token
        index += 1
    if url is None:
        raise ValueError("curl command does not contain a URL")
    if url.startswith("//"):
        url = f"http:{url}"
    elif "://" not in url:
        url = f"http://{url}"
    values: dict[str, object] = {"url": url}
    if method is not None:
        values["method"] = method
    elif body is not None:
        values["method"] = "POST"
    if headers:
        values["headers"] = headers
    if cookies:
        values["cookies"] = cookies
    if body is not None:
        values["body"] = body
    return values


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
    attributes: ClassVar[tuple[str, ...]] = (
        "url",
        "headers",
        "body",
        "cookies",
        "meta",
        "encoding",
        "flags",
        "cb_kwargs",
        "callback",
        "dont_filter",
        "errback",
        "method",
        "priority",
    )

    url: str
    callback: Callback | None = None
    method: str = "GET"
    headers: HeaderInput = field(default_factory=Headers)
    body: bytes | str | None = b""
    cookies: CookieInput = field(default_factory=dict)
    meta: dict[str, Any] = field(default_factory=dict)
    encoding: str = "utf-8"
    priority: int = 0
    dont_filter: bool = False
    errback: Errback | None = None
    flags: tuple[str, ...] = ()
    cb_kwargs: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        encoding = _normalized_encoding(self.encoding)
        object.__setattr__(self, "encoding", encoding)
        object.__setattr__(self, "url", _prepare_url(self.url, encoding))
        object.__setattr__(self, "method", str(self.method).upper())
        object.__setattr__(self, "body", _body_bytes(self.body, encoding))
        object.__setattr__(
            self,
            "headers",
            Headers(self.headers, encoding=encoding),
        )
        object.__setattr__(self, "cookies", _copy_cookies(self.cookies))
        object.__setattr__(self, "meta", dict(self.meta))
        object.__setattr__(self, "cb_kwargs", dict(self.cb_kwargs))
        object.__setattr__(self, "flags", tuple(self.flags))
        if not isinstance(self.priority, int):
            raise TypeError(f"Request priority not an integer: {self.priority!r}")
        if self.callback is not None and not callable(self.callback):
            raise TypeError(f"callback must be a callable, got {type(self.callback).__name__}")
        if self.errback is not None and not callable(self.errback):
            raise TypeError(f"errback must be a callable, got {type(self.errback).__name__}")

    def replace(
        self,
        *args: object,
        cls: type[Request] | None = None,
        **changes: object,
    ) -> Request:
        values = {name: getattr(self, name) for name in self.attributes}
        values.update(changes)
        request_type = self.__class__ if cls is None else cls
        return request_type(*args, **values)

    def copy(self) -> Request:
        return self.replace()

    def to_dict(self, *, spider: object | None = None) -> dict[str, Any]:
        values: dict[str, Any] = {
            "url": self.url,
            "callback": _callback_name(spider, self.callback, "callback"),
            "errback": _callback_name(spider, self.errback, "errback"),
            "headers": self.headers.to_scrapy_dict(),
        }
        for name in self.attributes:
            values.setdefault(name, getattr(self, name))
        values["flags"] = list(self.flags)
        if type(self) is not Request:
            values["_class"] = f"{self.__class__.__module__}.{self.__class__.__name__}"
        return values

    @classmethod
    def from_curl(
        cls,
        curl_command: str,
        ignore_unknown_options: bool = True,
        **kwargs: object,
    ) -> Request:
        values = _curl_request_kwargs(
            curl_command,
            ignore_unknown_options=ignore_unknown_options,
        )
        values.update(kwargs)
        return cls(**values)

    def follow(
        self,
        url: str,
        *,
        callback: Callback | None = None,
        method: str = "GET",
        headers: Headers | None = None,
        body: bytes = b"",
        cookies: CookieInput | None = None,
        meta: Mapping[str, Any] | None = None,
        encoding: str | None = None,
        priority: int | None = None,
        dont_filter: bool = False,
        errback: Errback | None = None,
        flags: Iterable[str] | None = None,
        cb_kwargs: Mapping[str, Any] | None = None,
    ) -> Request:
        return Request(
            url=urljoin(self.url, url),
            callback=callback,
            method=method,
            headers=headers or Headers(),
            body=body,
            cookies={} if cookies is None else cookies,
            meta=dict(meta or {}),
            encoding=self.encoding if encoding is None else encoding,
            priority=self.priority if priority is None else priority,
            dont_filter=dont_filter,
            errback=errback,
            flags=tuple(flags or ()),
            cb_kwargs=dict(cb_kwargs or {}),
        )

    def __repr__(self) -> str:
        return f"<{self.method} {self.url}>"


@dataclass(frozen=True, slots=True)
class Response:
    attributes: ClassVar[tuple[str, ...]] = (
        "url",
        "headers",
        "body",
        "flags",
        "status",
        "request",
        "certificate",
        "ip_address",
        "protocol",
    )

    url: str
    status: int = 200
    headers: HeaderInput = field(default_factory=Headers)
    body: bytes = b""
    request: Request | None = None
    flags: tuple[str, ...] = ()
    protocol: str | None = None
    certificate: object | None = None
    ip_address: IPv4Address | IPv6Address | None = None

    def __post_init__(self) -> None:
        _validate_url(self.url)
        object.__setattr__(self, "status", int(self.status))
        if not 100 <= self.status <= 599:
            raise ValueError("response status must be between 100 and 599")
        object.__setattr__(self, "headers", Headers(self.headers))
        if not isinstance(self.body, bytes):
            raise TypeError("Response body must be bytes. Use TextResponse for unicode bodies.")
        object.__setattr__(self, "flags", tuple(self.flags))

    @property
    def meta(self) -> dict[str, Any]:
        if self.request is None:
            raise AttributeError(
                "Response.meta not available, this response is not tied to any request"
            )
        return self.request.meta

    @property
    def cb_kwargs(self) -> dict[str, Any]:
        if self.request is None:
            raise AttributeError(
                "Response.cb_kwargs not available, this response is not tied to any request"
            )
        return self.request.cb_kwargs

    @property
    def text(self) -> str:
        raise AttributeError("Response content isn't text")

    def replace(
        self,
        *args: object,
        cls: type[Response] | None = None,
        **changes: object,
    ) -> Response:
        values = {name: getattr(self, name) for name in self.attributes}
        values.update(changes)
        response_type = self.__class__ if cls is None else cls
        return response_type(*args, **values)

    def copy(self) -> Response:
        return self.replace()

    def urljoin(self, url: str) -> str:
        return urljoin(self.url, url)

    def follow(
        self,
        url: str | object,
        callback: Callback | None = None,
        method: str = "GET",
        headers: HeaderInput | None = None,
        body: bytes | str | None = None,
        cookies: CookieInput | None = None,
        meta: Mapping[str, Any] | None = None,
        encoding: str | None = "utf-8",
        priority: int = 0,
        dont_filter: bool = False,
        errback: Errback | None = None,
        cb_kwargs: Mapping[str, Any] | None = None,
        flags: Iterable[str] | None = None,
    ) -> Request:
        if encoding is None:
            raise ValueError("encoding can't be None")
        if not isinstance(url, str):
            candidate = getattr(url, "url", None)
            if not isinstance(candidate, str):
                raise ValueError("url must be a string or an object with a string url")
            url = candidate
        return Request(
            url=self.urljoin(url),
            callback=callback,
            method=method,
            headers=headers or Headers(),
            body=body,
            cookies={} if cookies is None else cookies,
            meta=dict(meta or {}),
            encoding=encoding,
            priority=priority,
            dont_filter=dont_filter,
            errback=errback,
            cb_kwargs=dict(cb_kwargs or {}),
            flags=tuple(flags or ()),
        )

    def follow_all(
        self,
        urls: Iterable[str | object],
        **kwargs: object,
    ) -> Iterable[Request]:
        if not hasattr(urls, "__iter__"):
            raise TypeError("'urls' argument must be an iterable")
        return (self.follow(url, **kwargs) for url in urls)

    def __repr__(self) -> str:
        return f"<{self.status} {self.url}>"


@dataclass(frozen=True, slots=True, repr=False)
class TextResponse(Response):
    attributes: ClassVar[tuple[str, ...]] = (*Response.attributes, "encoding")

    encoding: str | None = None
    _cached_text: str | None = field(default=None, init=False, repr=False, compare=False)
    _cached_selector: Selector | None = field(default=None, init=False, repr=False, compare=False)
    _cached_base_url: str | None = field(default=None, init=False, repr=False, compare=False)
    _decode_content_type: str | None = field(default=None, init=False, repr=False, compare=False)
    _cached_json: object = field(
        default=_JSON_UNSET,
        init=False,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        if isinstance(self.body, str):
            if self.encoding is None:
                raise TypeError("Cannot convert unicode body; TextResponse has no encoding")
            object.__setattr__(
                self,
                "body",
                self.body.encode(_normalized_encoding(self.encoding)),
            )
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

    def body_as_unicode(self) -> str:
        return self.text

    def json(self, **kwargs: object) -> object:
        if kwargs:
            return json.loads(self.body, **kwargs)
        if self._cached_json is _JSON_UNSET:
            object.__setattr__(self, "_cached_json", json.loads(self.body))
        return self._cached_json

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
        kwargs.setdefault("encoding", self.encoding)
        return Response.follow(self, url, **kwargs)

    def follow_all(
        self,
        urls: Iterable[str | object] | SelectorList | None = None,
        *,
        css: str | None = None,
        xpath: str | None = None,
        **kwargs: object,
    ) -> Iterable[Request]:
        arguments = [value for value in (urls, css, xpath) if value is not None]
        if len(arguments) != 1:
            raise ValueError(
                "Please supply exactly one of the following arguments: urls, css, xpath"
            )
        selected: Iterable[object]
        if css is not None:
            selected = self.css(css)
        elif xpath is not None:
            selected = self.xpath(xpath)
        else:
            assert urls is not None
            selected = urls
        if isinstance(selected, SelectorList):
            extracted: list[str] = []
            for selector in selected:
                try:
                    extracted.append(_url_from_selector(selector))
                except ValueError:
                    continue
            selected = extracted
        kwargs.setdefault("encoding", self.encoding)
        return Response.follow_all(self, selected, **kwargs)

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


@dataclass(frozen=True, slots=True, repr=False)
class HtmlResponse(TextResponse):
    def _selector_type(self) -> str:
        return "html"


@dataclass(frozen=True, slots=True, repr=False)
class XmlResponse(TextResponse):
    def _selector_type(self) -> str:
        return "xml"


class FormRequest(Request):
    valid_form_methods: ClassVar[tuple[str, ...]] = ("GET", "POST")

    def __init__(
        self,
        *args: object,
        formdata: FormData | None = None,
        **kwargs: object,
    ) -> None:
        if formdata and len(args) < 3 and kwargs.get("method") is None:
            kwargs["method"] = "POST"
        super().__init__(*args, **kwargs)
        if not formdata:
            return
        query = urlencode(_form_pairs(formdata, self.encoding))
        if self.method == "POST":
            self.headers.setdefault(
                "Content-Type",
                b"application/x-www-form-urlencoded",
            )
            object.__setattr__(self, "body", query.encode("ascii"))
        else:
            parts = urlsplit(self.url)
            object.__setattr__(
                self,
                "url",
                urlunsplit((parts.scheme, parts.netloc, parts.path, query, parts.fragment)),
            )

    @classmethod
    def from_response(
        cls,
        response: TextResponse,
        formname: str | None = None,
        formid: str | None = None,
        formnumber: int = 0,
        formdata: FormData | None = None,
        clickdata: Mapping[str, str | int] | None = None,
        dont_click: bool = False,
        formxpath: str | None = None,
        formcss: str | None = None,
        **kwargs: object,
    ) -> FormRequest:
        warnings.warn(
            "FormRequest.from_response() is deprecated; prefer the form2request package",
            DeprecationWarning,
            stacklevel=2,
        )
        if formcss is not None and formxpath is not None:
            raise ValueError("formcss and formxpath cannot both be set")
        if formcss is not None:
            candidates = response.css(formcss)
        elif formxpath is not None:
            candidates = response.xpath(formxpath)
        else:
            candidates = response.xpath("//form")
        forms = [
            selector.root
            for selector in candidates
            if hasattr(selector.root, "tag")
            and selector.root.tag.lower() == "form"
            and (formname is None or selector.root.get("name") == formname)
            and (formid is None or selector.root.get("id") == formid)
        ]
        try:
            form = forms[formnumber]
        except IndexError as error:
            raise ValueError(f"no form found at index {formnumber}") from error

        controls: list[tuple[str, str]] = []
        clickables: list[object] = []
        for element in form.xpath(".//input | .//textarea | .//select | .//button"):
            if element.get("disabled") is not None:
                continue
            name = element.get("name")
            tag = element.tag.lower()
            input_type = element.get("type", "").lower() if tag == "input" else ""
            button_type = element.get("type", "submit").lower() if tag == "button" else ""
            if (tag == "button" and button_type == "submit") or input_type in {
                "submit",
                "image",
            }:
                clickables.append(element)
                continue
            if tag == "button" or name is None or input_type in {"button", "reset", "file"}:
                continue
            if input_type in {"checkbox", "radio"} and element.get("checked") is None:
                continue
            if tag == "textarea":
                controls.append((name, element.text or ""))
            elif tag == "select":
                options = element.xpath(".//option[@selected]")
                if not options and element.get("multiple") is None:
                    options = element.xpath(".//option[position() = 1]")
                controls.extend(
                    (name, option.get("value", "".join(option.itertext()))) for option in options
                )
            else:
                controls.append(
                    (
                        name,
                        element.get("value", "on" if input_type in {"checkbox", "radio"} else ""),
                    )
                )

        if not dont_click and clickables:
            selected = None
            if clickdata is None:
                selected = clickables[0]
            else:
                number = clickdata.get("nr")
                matches = [
                    element
                    for element in clickables
                    if all(
                        key == "nr" or element.get(key) == str(value)
                        for key, value in clickdata.items()
                    )
                ]
                if number is not None:
                    try:
                        selected = clickables[int(number)]
                    except (IndexError, ValueError) as error:
                        raise ValueError(f"invalid clickdata: {clickdata!r}") from error
                elif len(matches) == 1:
                    selected = matches[0]
                else:
                    raise ValueError(f"clickdata did not identify one control: {clickdata!r}")
            if selected is not None and selected.get("name") is not None:
                controls.append((selected.get("name"), selected.get("value", "")))

        if formdata is not None:
            override_items = formdata.items() if isinstance(formdata, Mapping) else formdata
            override_items = list(override_items)
            names = {
                name.decode(response.encoding) if isinstance(name, bytes) else str(name)
                for name, _ in override_items
            }
            controls = [(name, value) for name, value in controls if name not in names]
            overrides = _form_pairs(
                [(name, value) for name, value in override_items if value is not None],
                response.encoding,
            )
            controls.extend(
                (
                    name.decode(response.encoding),
                    value.decode(response.encoding),
                )
                for name, value in overrides
            )

        kwargs.setdefault("encoding", response.encoding)
        action = kwargs.pop("url", None) or form.get("action") or response.url
        method = str(kwargs.pop("method", form.get("method", "GET"))).upper()
        if method not in cls.valid_form_methods:
            method = "GET"
        return cls(
            url=response.urljoin(action),
            method=method,
            formdata=controls,
            **kwargs,
        )


class JsonRequest(Request):
    attributes: ClassVar[tuple[str, ...]] = (*Request.attributes, "dumps_kwargs")

    def __init__(
        self,
        *args: object,
        dumps_kwargs: dict[str, Any] | None = None,
        **kwargs: object,
    ) -> None:
        object.__setattr__(
            self,
            "_dumps_kwargs",
            copy.deepcopy(dumps_kwargs) if dumps_kwargs is not None else {},
        )
        self._dumps_kwargs.setdefault("sort_keys", True)
        body_passed = kwargs.get("body") is not None
        data = kwargs.pop("data", None)
        if body_passed and data is not None:
            warnings.warn("Both body and data passed. data will be ignored", stacklevel=2)
        elif not body_passed and data is not None:
            kwargs["body"] = self._dumps(data)
            kwargs.setdefault("method", "POST")
        super().__init__(*args, **kwargs)
        self.headers.setdefault("Content-Type", b"application/json")
        self.headers.setdefault(
            "Accept",
            b"application/json, text/javascript, */*; q=0.01",
        )

    @property
    def dumps_kwargs(self) -> dict[str, Any]:
        return self._dumps_kwargs

    def replace(
        self,
        *args: object,
        cls: type[Request] | None = None,
        **kwargs: object,
    ) -> Request:
        body_passed = kwargs.get("body") is not None
        data = kwargs.pop("data", None)
        if body_passed and data is not None:
            warnings.warn("Both body and data passed. data will be ignored", stacklevel=2)
        elif not body_passed and data is not None:
            kwargs["body"] = self._dumps(data)
        return super().replace(*args, cls=cls, **kwargs)

    def _dumps(self, data: object) -> str:
        return json.dumps(data, **self._dumps_kwargs)
