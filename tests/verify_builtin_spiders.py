from __future__ import annotations

import asyncio
import gzip
import inspect
import json
import os
import subprocess
import sys
from collections.abc import Iterable
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from spideroxide import (
    Crawler,
    CSVFeedSpider,
    Request,
    Response,
    Sitemap,
    SitemapSpider,
    TextResponse,
    XMLFeedSpider,
    XmlResponse,
)
from spideroxide.exceptions import NotSupported
from spideroxide.iterators import csviter, xmliter_lxml
from spideroxide.sitemap import iterloc, sitemap_urls_from_robots

URLSET = b"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"
        xmlns:xhtml="http://www.w3.org/1999/xhtml">
  <url>
    <loc>https://example.test/product/1</loc>
    <lastmod>2026-09-20</lastmod>
  </url>
  <url>
    <loc>https://example.test/product/2</loc>
    <lastmod>2026-09-21</lastmod>
    <xhtml:link rel="alternate" hreflang="fr"
                href="https://example.test/fr/product/2" />
  </url>
  <url>
    <loc>https://example.test/blog/1</loc>
    <lastmod>2026-09-22</lastmod>
  </url>
  <url>
    <loc>https://example.test/product/old</loc>
    <lastmod>2020-01-01</lastmod>
  </url>
  <url><lastmod>2026-09-23</lastmod></url>
</urlset>
"""

SITEMAP_INDEX = b"""<?xml version="1.0"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <sitemap><loc>https://example.test/nested.xml.gz</loc></sitemap>
  <sitemap><loc>https://example.test/ignored.xml</loc></sitemap>
</sitemapindex>
"""

PRODUCT_XML = b"""<?xml version="1.0"?>
<products>
  <product><name>Widget</name><price>10</price></product>
  <product><name>Gadget</name><price>20</price></product>
</products>
"""

NAMESPACED_XML = b"""<?xml version="1.0"?>
<p:products xmlns:p="https://example.test/products">
  <p:product><p:name>Namespaced</p:name></p:product>
</p:products>
"""

PRODUCT_CSV = b"""name;price
"Widget; Pro";10
broken
Raw;20
"""


def _public_api_snapshot() -> dict[str, object]:
    targets = {
        "CSVFeedSpider": CSVFeedSpider,
        "Sitemap": Sitemap,
        "SitemapSpider": SitemapSpider,
        "XMLFeedSpider": XMLFeedSpider,
        "csviter": csviter,
        "sitemap_urls_from_robots": sitemap_urls_from_robots,
        "xmliter_lxml": xmliter_lxml,
    }
    return {
        "defaults": {
            "csv_delimiter": CSVFeedSpider.delimiter,
            "csv_headers": CSVFeedSpider.headers,
            "csv_quotechar": CSVFeedSpider.quotechar,
            "sitemap_alternate_links": SitemapSpider.sitemap_alternate_links,
            "sitemap_follow": list(SitemapSpider.sitemap_follow),
            "sitemap_rules": [
                [pattern, callback] for pattern, callback in SitemapSpider.sitemap_rules
            ],
            "sitemap_urls": list(SitemapSpider.sitemap_urls),
            "xml_iterator": XMLFeedSpider.iterator,
            "xml_itertag": XMLFeedSpider.itertag,
            "xml_namespaces": list(XMLFeedSpider.namespaces),
        },
        "outputs": {
            "csv": list(csviter("first,second\n1,2\n")),
            "robots": list(
                sitemap_urls_from_robots(
                    b"Sitemap: /sitemap.xml",
                    "https://example.test/robots.txt",
                )
            ),
            "sitemap": list(
                Sitemap(b"<urlset><url><loc>https://example.test/</loc></url></urlset>")
            ),
            "xml": [
                node.xpath("name/text()").get()
                for node in xmliter_lxml(
                    b"<root><item><name>Widget</name></item></root>",
                    "item",
                )
            ],
        },
        "signatures": {
            name: [
                [parameter_name, parameter.kind.name]
                for parameter_name, parameter in inspect.signature(target).parameters.items()
            ]
            for name, target in targets.items()
        },
    }


def _verify_upstream_public_api() -> None:
    try:
        version("Scrapy")
    except PackageNotFoundError:
        return
    script = """
import inspect
import json

from scrapy.spiders import CSVFeedSpider, SitemapSpider, XMLFeedSpider
from scrapy.utils.iterators import csviter, xmliter_lxml
from scrapy.utils.sitemap import Sitemap, sitemap_urls_from_robots

targets = {
    "CSVFeedSpider": CSVFeedSpider,
    "Sitemap": Sitemap,
    "SitemapSpider": SitemapSpider,
    "XMLFeedSpider": XMLFeedSpider,
    "csviter": csviter,
    "sitemap_urls_from_robots": sitemap_urls_from_robots,
    "xmliter_lxml": xmliter_lxml,
}
snapshot = {
    "defaults": {
        "csv_delimiter": CSVFeedSpider.delimiter,
        "csv_headers": CSVFeedSpider.headers,
        "csv_quotechar": CSVFeedSpider.quotechar,
        "sitemap_alternate_links": SitemapSpider.sitemap_alternate_links,
        "sitemap_follow": list(SitemapSpider.sitemap_follow),
        "sitemap_rules": [
            [pattern, callback] for pattern, callback in SitemapSpider.sitemap_rules
        ],
        "sitemap_urls": list(SitemapSpider.sitemap_urls),
        "xml_iterator": XMLFeedSpider.iterator,
        "xml_itertag": XMLFeedSpider.itertag,
        "xml_namespaces": list(XMLFeedSpider.namespaces),
    },
    "outputs": {
        "csv": list(csviter("first,second\\n1,2\\n")),
        "robots": list(
            sitemap_urls_from_robots(
                b"Sitemap: /sitemap.xml",
                "https://example.test/robots.txt",
            )
        ),
        "sitemap": list(
            Sitemap(
                b"<urlset><url><loc>https://example.test/</loc></url></urlset>"
            )
        ),
        "xml": [
            node.xpath("name/text()").get()
            for node in xmliter_lxml(
                b"<root><item><name>Widget</name></item></root>",
                "item",
            )
        ],
    },
    "signatures": {
        name: [
            [parameter_name, parameter.kind.name]
            for parameter_name, parameter in inspect.signature(target).parameters.items()
        ]
        for name, target in targets.items()
    },
}
print(json.dumps(snapshot, sort_keys=True))
"""
    environment = dict(os.environ)
    environment.pop("PYTHONPATH", None)
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    local_snapshot = _public_api_snapshot()
    upstream_snapshot = json.loads(result.stdout)
    assert local_snapshot == upstream_snapshot, json.dumps(
        {"compat": local_snapshot, "scrapy": upstream_snapshot},
        indent=2,
        sort_keys=True,
    )


def _verify_sitemap_utilities() -> None:
    sitemap = Sitemap(URLSET)
    assert sitemap.type == "urlset"
    entries = list(sitemap)
    assert entries[0] == {
        "loc": "https://example.test/product/1",
        "lastmod": "2026-09-20",
    }
    assert entries[1]["alternate"] == ["https://example.test/fr/product/2"]
    assert len(entries) == 4
    assert list(iterloc(entries[:2], alt=True)) == [
        "https://example.test/product/1",
        "https://example.test/product/2",
        "https://example.test/fr/product/2",
    ]

    malformed = Sitemap(b"<urlset><url><loc>https://example.test/recovered</loc></url>")
    assert list(malformed) == [{"loc": "https://example.test/recovered"}]

    robots = (
        b"User-agent: *\n"
        b" Sitemap: /sitemap.xml\n"
        b"SITEMAP: https://cdn.example.test/sitemap.xml\n"
        b"Sitemap: \xff\n"
    )
    assert list(sitemap_urls_from_robots(robots, "https://example.test/robots.txt")) == [
        "https://example.test/sitemap.xml",
        "https://cdn.example.test/sitemap.xml",
    ]

    assert list(csviter("first,second\n1,2\nbad\n")) == [{"first": "1", "second": "2"}]
    assert list(csviter("1|2\n", delimiter="|", headers=["first", "second"])) == [
        {"first": "1", "second": "2"}
    ]
    nodes = list(xmliter_lxml(NAMESPACED_XML, "p:product"))
    assert nodes[0].xpath("p:name/text()").get() == "Namespaced"


class BuiltinSpiderDownloader:
    def __init__(self) -> None:
        self.urls: list[str] = []
        self.closed = False

    async def fetch(self, request: Request) -> Response:
        self.urls.append(request.url)
        if request.url.endswith("/robots.txt"):
            body = b"User-agent: *\nSitemap: /sitemap-index.xml\nSitemap: \xff\n"
            return Response(request.url, body=body, request=request)
        if request.url.endswith("/sitemap-index.xml"):
            return XmlResponse(
                request.url,
                body=SITEMAP_INDEX,
                encoding="utf-8",
                request=request,
            )
        if request.url.endswith("/nested.xml.gz"):
            return Response(
                request.url,
                body=gzip.compress(URLSET),
                request=request,
            )
        if request.url.endswith("/feed.xml"):
            return TextResponse(
                request.url,
                headers={"Content-Type": "application/xml"},
                body=PRODUCT_XML,
                encoding="utf-8",
                request=request,
            )
        if request.url.endswith("/namespaced.xml"):
            return TextResponse(
                request.url,
                headers={"Content-Type": "application/xml"},
                body=NAMESPACED_XML,
                encoding="utf-8",
                request=request,
            )
        if request.url.endswith("/feed.csv"):
            return TextResponse(
                request.url,
                headers={"Content-Type": "text/csv"},
                body=PRODUCT_CSV,
                encoding="utf-8",
                request=request,
            )
        return TextResponse(
            request.url,
            body=b"<html></html>",
            encoding="utf-8",
            request=request,
        )

    async def close(self) -> None:
        self.closed = True


class ProductSitemapSpider(SitemapSpider):
    name = "product-sitemap"
    sitemap_urls = ("https://example.test/robots.txt",)
    sitemap_follow = (r"/nested\.xml\.gz$",)
    sitemap_rules = (
        (r"/(?:fr/)?product/", "parse_product"),
        (r"/blog/", "parse_blog"),
    )
    sitemap_alternate_links = True

    def sitemap_filter(
        self,
        entries: Iterable[dict[str, object]],
    ) -> Iterable[dict[str, object]]:
        for entry in entries:
            if str(entry.get("lastmod", "9999")) >= "2025":
                yield entry

    def parse_product(self, response: Response) -> dict[str, str]:
        return {"kind": "product", "url": response.url}

    def parse_blog(self, response: Response) -> dict[str, str]:
        return {"kind": "blog", "url": response.url}


class ProductXMLSpider(XMLFeedSpider):
    name = "product-xml"
    start_urls = ("https://example.test/feed.xml",)
    itertag = "product"

    def adapt_response(self, response: Response) -> Response:
        return response.replace(body=response.body.replace(b"Gadget", b"Adapted"))

    def parse_node(self, response: Response, selector) -> dict[str, str]:
        return {
            "kind": "xml",
            "name": selector.xpath("name/text()").get(),
        }

    def process_results(
        self,
        response: Response,
        results: Iterable[object],
    ) -> Iterable[object]:
        for result in results:
            assert isinstance(result, dict)
            yield {**result, "processed": "yes"}


class NamespacedXMLSpider(XMLFeedSpider):
    name = "namespaced-xml"
    start_urls = ("https://example.test/namespaced.xml",)
    itertag = "p:product"
    namespaces = (("p", "https://example.test/products"),)

    def parse_node(self, response: Response, selector) -> dict[str, str]:
        return {
            "kind": "xml",
            "name": selector.xpath("p:name/text()").get(),
        }


class ProductCSVSpider(CSVFeedSpider):
    name = "product-csv"
    start_urls = ("https://example.test/feed.csv",)
    delimiter = ";"
    quotechar = '"'

    def adapt_response(self, response: Response) -> Response:
        return response.replace(body=response.body.replace(b"Raw", b"Adapted"))

    def parse_row(self, response: Response, row: dict[str, str]) -> dict[str, str]:
        return {"kind": "csv", **row}

    def process_results(
        self,
        response: Response,
        results: Iterable[object],
    ) -> Iterable[object]:
        for result in results:
            assert isinstance(result, dict)
            yield {**result, "processed": "yes"}


class LegacyXMLSpider(XMLFeedSpider):
    name = "legacy-xml"
    itertag = "product"

    def parse_item(self, response: Response, selector) -> dict[str, str]:
        return {"name": selector.xpath("name/text()").get()}


def _verify_feed_hooks_and_errors() -> None:
    response = TextResponse(
        "https://example.test/feed.xml",
        body=PRODUCT_XML,
        encoding="utf-8",
    )
    legacy = LegacyXMLSpider()
    assert list(legacy._parse(response)) == [{"name": "Widget"}, {"name": "Gadget"}]

    html_spider = LegacyXMLSpider()
    html_spider.iterator = "html"
    assert list(html_spider._parse(response)) == [
        {"name": "Widget"},
        {"name": "Gadget"},
    ]

    unsupported = LegacyXMLSpider()
    unsupported.iterator = "unsupported"
    try:
        unsupported._parse(response)
    except NotSupported as error:
        assert str(error) == "Unsupported node iterator"
    else:
        raise AssertionError("unsupported XML iterator was accepted")

    xml_iterator = LegacyXMLSpider()
    xml_iterator.iterator = "xml"
    try:
        xml_iterator._parse(Response("https://example.test/feed.xml", body=PRODUCT_XML))
    except ValueError as error:
        assert str(error) == "Response content isn't text"
    else:
        raise AssertionError("XML iterator accepted a binary response")

    sitemap_spider = ProductSitemapSpider()
    compressed = gzip.compress(URLSET)
    limited = Response(
        "https://example.test/nested.xml.gz",
        body=compressed,
        request=Request(
            "https://example.test/nested.xml.gz",
            meta={"download_maxsize": 20},
        ),
    )
    assert sitemap_spider._get_sitemap_body(limited) is None
    already_decompressed = Response(
        "https://example.test/nested.xml.gz",
        body=URLSET,
    )
    assert sitemap_spider._get_sitemap_body(already_decompressed) == URLSET
    assert (
        sitemap_spider._get_sitemap_body(
            Response("https://example.test/not-a-sitemap", body=URLSET)
        )
        is None
    )


async def _crawl(
    spider: type[SitemapSpider | XMLFeedSpider | CSVFeedSpider],
    engine: str,
) -> tuple[tuple[object, ...], list[str]]:
    downloader = BuiltinSpiderDownloader()
    result = await Crawler(
        spider,
        {
            "ENGINE_BACKEND": engine,
            "CONCURRENT_REQUESTS": 1,
            "RETRY_ENABLED": False,
        },
        downloader=downloader,
    ).crawl()
    assert result.reason == "finished"
    assert downloader.closed
    return result.items, downloader.urls


async def _verify_engine(engine: str) -> None:
    sitemap_items, sitemap_urls = await _crawl(ProductSitemapSpider, engine)
    assert sorted(sitemap_items, key=lambda item: item["url"]) == [
        {"kind": "blog", "url": "https://example.test/blog/1"},
        {"kind": "product", "url": "https://example.test/fr/product/2"},
        {"kind": "product", "url": "https://example.test/product/1"},
        {"kind": "product", "url": "https://example.test/product/2"},
    ]
    assert "https://example.test/ignored.xml" not in sitemap_urls
    assert "https://example.test/product/old" not in sitemap_urls

    xml_items, _ = await _crawl(ProductXMLSpider, engine)
    assert xml_items == (
        {"kind": "xml", "name": "Widget", "processed": "yes"},
        {"kind": "xml", "name": "Adapted", "processed": "yes"},
    )

    namespaced_items, _ = await _crawl(NamespacedXMLSpider, engine)
    assert namespaced_items == ({"kind": "xml", "name": "Namespaced"},)

    csv_items, _ = await _crawl(ProductCSVSpider, engine)
    assert csv_items == (
        {
            "kind": "csv",
            "name": "Widget; Pro",
            "price": "10",
            "processed": "yes",
        },
        {
            "kind": "csv",
            "name": "Adapted",
            "price": "20",
            "processed": "yes",
        },
    )


async def _verify() -> None:
    _verify_upstream_public_api()
    _verify_sitemap_utilities()
    _verify_feed_hooks_and_errors()
    for engine in ("python", "rust"):
        await _verify_engine(engine)


if __name__ == "__main__":
    asyncio.run(_verify())
    print(
        "Built-in spiders passed: sitemap discovery, nested and compressed maps, alternate "
        "links, callback routing, XML namespaces and iterators, CSV options, hooks, malformed "
        "inputs, and Python/Rust engine parity"
    )
