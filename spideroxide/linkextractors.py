from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from re import Pattern
from typing import Any
from urllib.parse import urljoin, urlsplit

from parsel import Selector
from w3lib.url import canonicalize_url, safe_url_string

from .http import TextResponse

RegexInput = str | Pattern[str] | Iterable[str | Pattern[str]]
Candidate = tuple[str, str, bool]

IGNORED_EXTENSIONS = frozenset(
    {
        "3gp",
        "7z",
        "7zip",
        "aac",
        "ai",
        "aiff",
        "apk",
        "asf",
        "asx",
        "au",
        "avi",
        "bat",
        "bin",
        "bmp",
        "bz2",
        "cdr",
        "cpl",
        "css",
        "dmg",
        "doc",
        "docb",
        "docm",
        "docx",
        "dotm",
        "dotx",
        "drw",
        "dxf",
        "eps",
        "exe",
        "flv",
        "gif",
        "hta",
        "ico",
        "iso",
        "jar",
        "jpeg",
        "jpg",
        "js",
        "m4a",
        "m4v",
        "mid",
        "mng",
        "mov",
        "mp3",
        "mp4",
        "mpg",
        "msi",
        "msp",
        "odg",
        "odp",
        "ods",
        "odt",
        "ogg",
        "pct",
        "pdf",
        "png",
        "potm",
        "potx",
        "pps",
        "ppt",
        "pptm",
        "pptx",
        "ps",
        "psp",
        "pst",
        "py",
        "qt",
        "ra",
        "rar",
        "rb",
        "rm",
        "rss",
        "sh",
        "svg",
        "swf",
        "tar",
        "tar.gz",
        "tif",
        "tiff",
        "wav",
        "webm",
        "webp",
        "wma",
        "wmv",
        "xls",
        "xlsm",
        "xlsx",
        "xltm",
        "xltx",
        "xz",
        "zip",
    }
)


def _values(value: object) -> tuple[object, ...]:
    if isinstance(value, (str, bytes)) or hasattr(value, "search"):
        return (value,)
    return tuple(value) if isinstance(value, Iterable) else (value,)


def _strings(value: str | Iterable[str]) -> tuple[str, ...]:
    return tuple(str(item) for item in _values(value))


def _regexes(value: RegexInput | None) -> tuple[Pattern[str], ...]:
    if value is None:
        return ()
    return tuple(
        item if hasattr(item, "search") else re.compile(str(item)) for item in _values(value)
    )


def _matches_domain(hostname: str, domain: str) -> bool:
    domain = domain.lower()
    return hostname == domain or hostname.endswith(f".{domain}")


@dataclass(slots=True, unsafe_hash=True)
class Link:
    url: str
    text: str = ""
    fragment: str = ""
    nofollow: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.url, str):
            raise TypeError(f"Link urls must be str objects, got {type(self.url).__name__}")


class LinkExtractor:
    def __init__(
        self,
        allow: RegexInput = (),
        deny: RegexInput = (),
        allow_domains: str | Iterable[str] = (),
        deny_domains: str | Iterable[str] = (),
        restrict_xpaths: str | Iterable[str] = (),
        tags: str | Iterable[str] = ("a", "area"),
        attrs: str | Iterable[str] = ("href",),
        canonicalize: bool = False,
        unique: bool = True,
        process_value: Callable[[Any], Any] | None = None,
        deny_extensions: str | Iterable[str] | None = None,
        restrict_css: str | Iterable[str] = (),
        strip: bool = True,
        restrict_text: RegexInput | None = None,
        deny_tags: str | Iterable[str] = (),
        deny_attrs: str | Iterable[str] = (),
    ) -> None:
        self.allow_res = _regexes(allow)
        self.deny_res = _regexes(deny)
        self.allow_domains = tuple(domain.lower() for domain in _strings(allow_domains))
        self.deny_domains = tuple(domain.lower() for domain in _strings(deny_domains))
        self.restrict_xpaths = _strings(restrict_xpaths)
        self.tags = _strings(tags)
        self.attrs = _strings(attrs)
        self.canonicalize = bool(canonicalize)
        self.unique = bool(unique)
        self.process_value = process_value
        extensions = IGNORED_EXTENSIONS if deny_extensions is None else _strings(deny_extensions)
        self.deny_extensions = frozenset(
            f".{extension.lower().lstrip('.')}" for extension in extensions
        )
        self.restrict_css = _strings(restrict_css)
        self.strip = bool(strip)
        self.restrict_text = _regexes(restrict_text)
        self.deny_tags = _strings(deny_tags)
        self.deny_attrs = _strings(deny_attrs)

    def extract_links(self, response: TextResponse) -> list[Link]:
        if not isinstance(response, TextResponse):
            raise TypeError(f"response must be TextResponse, got {type(response).__name__}")
        regions = self._regions(response)
        links: list[Link] = []
        seen: set[str] = set()
        for region in regions:
            for raw_url, text, nofollow in self._extract_candidates(region):
                try:
                    value: object = response.urljoin(raw_url)
                except ValueError:
                    continue
                if self.process_value is not None:
                    value = self.process_value(value)
                    if value is None:
                        continue
                try:
                    normalized = safe_url_string(value, encoding=response.encoding)
                    url = urljoin(response.url, normalized)
                except ValueError:
                    continue
                if not self._link_allowed(url, text):
                    continue
                if self.canonicalize:
                    url = canonicalize_url(url)
                    dedupe_key = url
                else:
                    dedupe_key = url
                if self.unique and dedupe_key in seen:
                    continue
                seen.add(dedupe_key)
                links.append(Link(url, text, nofollow=nofollow))
        return links

    def matches(self, url: str) -> bool:
        if self.allow_res and not any(regex.search(url) for regex in self.allow_res):
            return False
        if any(regex.search(url) for regex in self.deny_res):
            return False
        netloc = urlsplit(url).netloc.lower()
        if self.allow_domains and not any(
            _matches_domain(netloc, domain) for domain in self.allow_domains
        ):
            return False
        return not any(_matches_domain(netloc, domain) for domain in self.deny_domains)

    def _regions(self, response: TextResponse) -> list[str | Selector]:
        selectors: list[Selector] = []
        for xpath in self.restrict_xpaths:
            selectors.extend(response.xpath(xpath))
        for css in self.restrict_css:
            selectors.extend(response.css(css))
        if self.restrict_xpaths or self.restrict_css:
            return selectors
        return [response.text]

    def _extract_candidates(self, html: str | Selector) -> list[Candidate]:
        if isinstance(html, Selector):
            return self._extract_candidates_python(html)
        if "*" in self.attrs:
            return self._extract_candidates_python(html)
        try:
            from ._native import extract_link_candidates
        except (ImportError, AttributeError):
            return self._extract_candidates_python(html)
        candidates, malformed = extract_link_candidates(
            html,
            list(self.tags),
            list(self.attrs),
            list(self.deny_tags),
            list(self.deny_attrs),
            self.strip,
        )
        return self._extract_candidates_python(html) if malformed else list(candidates)

    def _extract_candidates_python(self, html: str | Selector) -> list[Candidate]:
        selector = html if isinstance(html, Selector) else Selector(text=html, type="html")
        tags = {tag.lower() for tag in self.tags}
        all_tags = "*" in tags
        denied_tags = {tag.lower() for tag in self.deny_tags}
        denied_attrs = {attr.lower() for attr in self.deny_attrs}
        all_attrs = "*" in {attr.lower() for attr in self.attrs}
        candidates: list[Candidate] = []
        for element in selector.xpath("descendant-or-self::*"):
            root = element.root
            tag = str(getattr(root, "tag", "")).lower()
            if (not all_tags and tag not in tags) or tag in denied_tags:
                continue
            text = "".join(root.itertext())
            rel = root.attrib.get("rel", "")
            nofollow = any(value.lower() == "nofollow" for value in rel.replace(",", " ").split())
            attrs = root.attrib if all_attrs else self.attrs
            for attr in attrs:
                if attr.lower() in denied_attrs:
                    continue
                value = root.attrib.get(attr)
                if value is None:
                    continue
                if self.strip:
                    value = value.strip(" \t\n\r\f")
                candidates.append((value, text, nofollow))
        return candidates

    def _link_allowed(self, url: str, text: str) -> bool:
        if self.allow_res and not any(regex.search(url) for regex in self.allow_res):
            return False
        if any(regex.search(url) for regex in self.deny_res):
            return False
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https", "file", "ftp"}:
            return False
        netloc = parsed.netloc.lower()
        if self.allow_domains and not any(
            _matches_domain(netloc, domain) for domain in self.allow_domains
        ):
            return False
        if any(_matches_domain(netloc, domain) for domain in self.deny_domains):
            return False
        path = parsed.path.lower()
        if self.deny_extensions and any(
            path.endswith(extension) for extension in self.deny_extensions
        ):
            return False
        return not self.restrict_text or any(regex.search(text) for regex in self.restrict_text)


LxmlLinkExtractor = LinkExtractor

__all__ = [
    "IGNORED_EXTENSIONS",
    "Link",
    "LinkExtractor",
    "LxmlLinkExtractor",
]
