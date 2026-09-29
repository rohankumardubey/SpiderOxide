from __future__ import annotations

import gzip
import logging
import re
from collections.abc import AsyncIterator, Iterable, Iterator, Sequence
from io import BytesIO
from typing import Any
from urllib.parse import urljoin

from lxml import etree

from .http import Request, Response, XmlResponse
from .spider import Spider

logger = logging.getLogger(__name__)


class Sitemap:
    __slots__ = ("type", "xmliter")

    def __init__(self, xmltext: str | bytes) -> None:
        if isinstance(xmltext, str):
            xmltext = xmltext.encode()
        self.xmliter = etree.iterparse(
            BytesIO(xmltext),
            recover=True,
            remove_comments=True,
            resolve_entities=False,
            remove_blank_text=True,
            collect_ids=False,
            remove_pis=True,
            events=("start", "end"),
        )
        _, root = next(self.xmliter)
        self.type = self._get_tag_name(root)

    def __iter__(self) -> Iterator[dict[str, Any]]:
        for event, element in self.xmliter:
            if event == "start" or self._get_tag_name(element) not in {"url", "sitemap"}:
                continue
            entry = self._process_sitemap_element(element)
            if entry:
                yield entry

    def _process_sitemap_element(
        self,
        element: etree._Element,
    ) -> dict[str, Any] | None:
        entry: dict[str, Any] = {}
        alternate: list[str] = []
        has_location = False
        for child in element:
            try:
                tag_name = self._get_tag_name(child)
                if not tag_name:
                    continue
                if tag_name == "link":
                    href = child.get("href")
                    if href:
                        alternate.append(href)
                else:
                    entry[tag_name] = child.text.strip() if child.text else ""
                    if not has_location and tag_name == "loc":
                        has_location = True
            finally:
                child.clear()
        element.clear()
        parent = element.getparent()
        if parent is not None:
            while element.getprevious() is not None:
                del parent[0]
        if not has_location:
            return None
        if alternate:
            entry["alternate"] = alternate
        return entry

    @staticmethod
    def _get_tag_name(element: etree._Element) -> str:
        tag = element.tag
        if not isinstance(tag, str):
            return ""
        _, _, local_name = tag.partition("}")
        return local_name or tag


def sitemap_urls_from_robots(
    robots_text: str | bytes,
    base_url: str | None = None,
) -> Iterable[str]:
    lines: Iterable[str | bytes]
    lines = BytesIO(robots_text) if isinstance(robots_text, bytes) else robots_text.splitlines()
    for line in lines:
        if isinstance(line, bytes):
            if line.lstrip()[:8].lower() != b"sitemap:":
                continue
            try:
                url = line.partition(b":")[2].strip().decode()
            except UnicodeDecodeError:
                continue
        else:
            if line.lstrip()[:8].lower() != "sitemap:":
                continue
            url = line.partition(":")[2].strip()
        yield urljoin(base_url or "", url)


def regex(value: re.Pattern[str] | str) -> re.Pattern[str]:
    return re.compile(value) if isinstance(value, str) else value


def iterloc(entries: Iterable[dict[str, Any]], alt: bool = False) -> Iterable[str]:
    for entry in entries:
        location = entry["loc"]
        if location:
            yield location
        if alt:
            yield from entry.get("alternate", ())


def _gunzip(body: bytes, max_size: int) -> bytes | None:
    with gzip.GzipFile(fileobj=BytesIO(body)) as compressed:
        if max_size:
            content = compressed.read(max_size + 1)
            return None if len(content) > max_size else content
        return compressed.read()


class SitemapSpider(Spider):
    sitemap_urls: Sequence[str] = ()
    sitemap_rules: Sequence[tuple[re.Pattern[str] | str, str | object]] = [("", "parse")]
    sitemap_follow: Sequence[re.Pattern[str] | str] = [""]
    sitemap_alternate_links = False

    def __init__(self, *a: object, **kw: object) -> None:
        super().__init__(*a, **kw)
        self._cbs: list[tuple[re.Pattern[str], object]] = []
        for pattern, callback in self.sitemap_rules:
            resolved = getattr(self, callback) if isinstance(callback, str) else callback
            self._cbs.append((regex(pattern), resolved))
        self._follow = [regex(pattern) for pattern in self.sitemap_follow]
        self._max_size = 0
        self._warn_size = 0

    @classmethod
    def from_crawler(
        cls,
        crawler: object,
        *args: object,
        **kwargs: object,
    ) -> SitemapSpider:
        spider = super().from_crawler(crawler, *args, **kwargs)
        settings = crawler.settings  # type: ignore[attr-defined]
        spider._max_size = getattr(
            spider,
            "download_maxsize",
            settings.getint("DOWNLOAD_MAXSIZE"),
        )
        spider._warn_size = getattr(
            spider,
            "download_warnsize",
            settings.getint("DOWNLOAD_WARNSIZE"),
        )
        return spider

    async def start(self) -> AsyncIterator[Request]:
        for url in self.sitemap_urls:
            yield Request(url, callback=self._parse_sitemap)

    def sitemap_filter(
        self,
        entries: Iterable[dict[str, Any]],
    ) -> Iterable[dict[str, Any]]:
        yield from entries

    def _parse_sitemap(self, response: Response) -> Iterable[Request]:
        if response.url.endswith("/robots.txt"):
            urls = sitemap_urls_from_robots(response.body, base_url=response.url)
            return (Request(url, callback=self._parse_sitemap) for url in urls)
        body = self._get_sitemap_body(response)
        if not body:
            logger.warning("Ignoring invalid sitemap: %r", response)
            return ()
        sitemap = Sitemap(body)
        if sitemap.type == "sitemapindex":
            urls = self._get_urls_from_sitemapindex(self.sitemap_filter(sitemap))
            return (Request(url, callback=self._parse_sitemap) for url in urls)
        if sitemap.type == "urlset":
            pairs = self._get_urls_and_callbacks_from_urlset(self.sitemap_filter(sitemap))
            return (Request(url, callback=callback) for url, callback in pairs)
        logger.warning("Ignoring invalid sitemap: %r", response)
        return ()

    def _get_urls_from_sitemapindex(
        self,
        entries: Iterable[dict[str, Any]],
    ) -> Iterable[str]:
        for location in iterloc(entries, self.sitemap_alternate_links):
            if any(pattern.search(location) for pattern in self._follow):
                yield location

    def _get_urls_and_callbacks_from_urlset(
        self,
        entries: Iterable[dict[str, Any]],
    ) -> Iterable[tuple[str, object]]:
        for location in iterloc(entries, self.sitemap_alternate_links):
            for pattern, callback in self._cbs:
                if pattern.search(location):
                    yield location, callback
                    break

    def _get_sitemap_body(self, response: Response) -> bytes | None:
        if isinstance(response, XmlResponse):
            return response.body
        if response.body.startswith(b"\x1f\x8b"):
            compressed_size = len(response.body)
            max_size = int(response.meta.get("download_maxsize", self._max_size))
            warn_size = int(response.meta.get("download_warnsize", self._warn_size))
            body = _gunzip(response.body, max_size)
            if body is None:
                return None
            if compressed_size < warn_size <= len(body):
                logger.warning(
                    "%r body size after decompression (%d B) is larger than "
                    "the download warning size (%d B).",
                    response,
                    len(body),
                    warn_size,
                )
            return body
        if response.url.endswith((".xml", ".xml.gz")):
            return response.body
        return None


__all__ = [
    "Sitemap",
    "SitemapSpider",
    "iterloc",
    "regex",
    "sitemap_urls_from_robots",
]
