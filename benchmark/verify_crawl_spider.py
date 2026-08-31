from __future__ import annotations

import asyncio
import sys
from collections.abc import Iterable
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from spideroxide._native import extract_link_candidates

from spideroxide import (
    Crawler,
    CrawlSpider,
    HtmlResponse,
    Link,
    LinkExtractor,
    Request,
    Rule,
)
from spideroxide.job import deserialize_request, serialize_request

HTML = """<!doctype html>
<html>
  <head><base href="https://example.test/root/"></head>
  <body>
    <nav><a href="/category">Category</a></nav>
    <main class="products">
      <a href=" product/1?b=2&a=1#details " rel="NOFOLLOW"> Product One </a>
      <a href="/product/2">Product Two</a>
      <a href="/manual.pdf">Manual</a>
      <a href="https://blocked.test/product/3">Blocked</a>
      <a href="/product/broken">Broken</a>
    </main>
  </body>
</html>
"""


def _verify_link_extractor() -> None:
    response = HtmlResponse(
        "https://example.test/start",
        body=HTML.encode(),
        encoding="utf-8",
    )
    extractor = LinkExtractor(
        allow=r"/product/",
        allow_domains="example.test",
        restrict_css=".products",
        deny=r"/(?:2|broken)$",
    )
    assert extractor.extract_links(response) == [
        Link(
            "https://example.test/root/product/1?b=2&a=1#details",
            " Product One ",
            nofollow=True,
        )
    ]

    canonical = LinkExtractor(
        canonicalize=True,
        deny_extensions=(),
        restrict_xpaths="//main",
    ).extract_links(response)
    assert canonical[0].url == "https://example.test/root/product/1?a=1&b=2"
    assert all(not link.url.endswith(".pdf") for link in extractor.extract_links(response))

    processed_values: list[str] = []

    def process_value(url: str) -> str | None:
        processed_values.append(url)
        return "/rewritten" if url.endswith("/product/2") else None

    processed = LinkExtractor(
        process_value=process_value,
        deny_extensions=(),
        restrict_css=".products",
    ).extract_links(response)
    assert processed == [Link("https://example.test/rewritten", "Product Two")]
    assert processed_values[0].startswith("https://example.test/root/product/1")

    native = extractor._extract_candidates(HTML)
    python = extractor._extract_candidates_python(HTML)
    assert native == python
    native_direct, malformed = extract_link_candidates(
        HTML,
        ["a", "area"],
        ["href"],
        [],
        [],
        True,
    )
    assert malformed is False
    assert list(native_direct) == LinkExtractor(deny_extensions=())._extract_candidates_python(HTML)

    mutable = Link("https://example.test/old")
    mutable.url = "https://example.test/new"
    assert mutable.url.endswith("/new")
    assert LinkExtractor().matches("https://example.test/archive.pdf")
    assert LinkExtractor(allow_domains="example.test:8443").matches(
        "https://example.test:8443/page"
    )
    assert not LinkExtractor(allow_domains="example.test").matches("https://example.test:8443/page")

    wildcard_response = HtmlResponse(
        "https://example.test/",
        body=b'<div class="box" data-url="/wild">Wild</div>',
        encoding="utf-8",
    )
    wildcard = LinkExtractor(
        tags="*",
        attrs="*",
        deny_attrs=("class",),
        deny_extensions=(),
    ).extract_links(wildcard_response)
    assert wildcard == [Link("https://example.test/wild", "Wild")]

    query_order = HtmlResponse(
        "https://example.test/",
        body=(
            b'<a href="/query?a=1&b=2">First</a>'
            b'<a href="/query?b=2&a=1" rel="external,nofollow">Second</a>'
        ),
        encoding="utf-8",
    )
    assert LinkExtractor(deny_extensions=()).extract_links(query_order) == [
        Link("https://example.test/query?a=1&b=2", "First"),
        Link(
            "https://example.test/query?b=2&a=1",
            "Second",
            nofollow=True,
        ),
    ]

    malformed = HtmlResponse(
        "https://example.test/",
        body=b'<a href="http://[bad">Bad</a><a href="/good">Good</a>',
        encoding="utf-8",
    )
    assert LinkExtractor(deny_extensions=()).extract_links(malformed) == [
        Link("https://example.test/good", "Good")
    ]

    based = HtmlResponse(
        "https://origin.test/path/index",
        body=b'<base href="https://other.test/base/"><a href="in">In</a>',
        encoding="utf-8",
    )
    rewritten = LinkExtractor(
        process_value=lambda url: "../out",
        deny_extensions=(),
    ).extract_links(based)
    assert rewritten == [Link("https://origin.test/out", "In")]
    try:
        LinkExtractor(
            process_value=lambda url: (_ for _ in ()).throw(ValueError("process callback failed")),
            deny_extensions=(),
        ).extract_links(based)
    except ValueError as error:
        assert str(error) == "process callback failed"
    else:
        raise AssertionError("process_value exception was swallowed")

    spider = ProductSpider()
    crawl_response = HtmlResponse(
        "https://example.test/start",
        body=HTML.encode(),
        encoding="utf-8",
        request=Request("https://example.test/start"),
    )
    persisted = next(
        request
        for request in spider._requests_to_follow(crawl_response)
        if request is not None and request.errback is not None
    )
    payload = serialize_request(persisted, spider)
    restored = deserialize_request(payload, spider)
    assert restored.callback == spider._callback
    assert restored.errback == spider._errback
    assert restored.cb_kwargs == {}


class MappingDownloader:
    def __init__(self) -> None:
        self.urls: list[str] = []
        self.closed = False

    async def fetch(self, request: Request) -> HtmlResponse:
        self.urls.append(request.url)
        if request.url.endswith("/product/broken"):
            raise ValueError("product download failed")
        pages = {
            "https://example.test/start": HTML,
            "https://example.test/category": (
                '<main class="products"><a href="/product/2">Product Two</a></main>'
            ),
            "https://example.test/root/product/1?b=2&a=1#details": "<html></html>",
            "https://example.test/product/2": "<html></html>",
        }
        return HtmlResponse(
            request.url,
            body=pages[request.url].encode(),
            encoding="utf-8",
            request=request,
        )

    async def close(self) -> None:
        self.closed = True


class ProductSpider(CrawlSpider):
    name = "products"
    start_urls = ["https://example.test/start"]
    rules = (
        Rule(
            LinkExtractor(allow=r"/category$", deny_extensions=()),
            callback="parse_category",
            follow=True,
        ),
        Rule(
            LinkExtractor(
                allow=r"/product/",
                allow_domains="example.test",
                deny_extensions=(),
            ),
            callback="parse_product",
            cb_kwargs={"source": "rule"},
            process_links="process_product_links",
            process_request="process_product_request",
            errback="handle_product_error",
        ),
    )

    def parse_start_url(self, response: HtmlResponse) -> dict[str, str]:
        return {"kind": "start"}

    async def parse_category(
        self,
        response: HtmlResponse,
    ) -> dict[str, str]:
        return {"kind": "category"}

    async def parse_product(
        self,
        response: HtmlResponse,
        source: str,
    ) -> dict[str, object]:
        return {
            "kind": "product",
            "url": response.url,
            "source": source,
            "link_text": response.meta["link_text"],
            "priority": response.request.priority if response.request else None,
            "request_hook": (
                response.request.headers.get("X-Rule").decode()
                if response.request is not None
                else None
            ),
        }

    def process_product_links(self, links: list[Link]) -> Iterable[Link]:
        return reversed(links)

    def process_product_request(
        self,
        request: Request,
        response: HtmlResponse,
    ) -> Request | None:
        if request.url.endswith("/product/2") and response.url.endswith("/start"):
            return None
        cb_kwargs = (
            {"source": "request"}
            if request.url.endswith("/product/1?b=2&a=1#details")
            else request.cb_kwargs
        )
        return request.replace(priority=7, cb_kwargs=cb_kwargs)

    def _build_request(self, rule_index: int, link: Link) -> Request:
        return super()._build_request(rule_index, link).replace(headers={"X-Rule": "applied"})

    def handle_product_error(self, exception: Exception) -> dict[str, str]:
        return {"kind": "error", "message": str(exception)}

    def process_results(
        self,
        response: HtmlResponse,
        results: object,
    ) -> object:
        if isinstance(results, dict):
            return {**results, "processed": True}
        return results


class AsyncStartProductSpider(ProductSpider):
    name = "async-start-products"

    async def start(self):
        yield Request("https://example.test/start")


class ParseHookSpider(ProductSpider):
    name = "parse-hook-products"

    async def parse_with_rules(
        self,
        response: HtmlResponse,
        callback: object,
        cb_kwargs: dict[str, object],
        follow: bool = True,
    ):
        async for output in super().parse_with_rules(
            response,
            callback,
            cb_kwargs,
            follow,
        ):
            if isinstance(output, dict):
                yield {**output, "parse_hook": True}
            else:
                yield output


async def _verify_engine(engine: str) -> None:
    downloader = MappingDownloader()
    result = await Crawler(
        ProductSpider,
        {
            "ENGINE_BACKEND": engine,
            "CONCURRENT_REQUESTS": 1,
            "RETRY_ENABLED": False,
        },
        downloader=downloader,
    ).crawl()
    assert downloader.closed
    assert result.reason == "finished"
    assert result.items == (
        {"kind": "start", "processed": True},
        {
            "kind": "product",
            "url": "https://example.test/root/product/1?b=2&a=1#details",
            "source": "request",
            "link_text": " Product One ",
            "priority": 7,
            "request_hook": "applied",
            "processed": True,
        },
        {"kind": "error", "message": "product download failed"},
        {"kind": "category", "processed": True},
        {
            "kind": "product",
            "url": "https://example.test/product/2",
            "source": "rule",
            "link_text": "Product Two",
            "priority": 7,
            "request_hook": "applied",
            "processed": True,
        },
    )

    disabled_downloader = MappingDownloader()
    disabled = await Crawler(
        ProductSpider,
        {
            "ENGINE_BACKEND": engine,
            "CRAWLSPIDER_FOLLOW_LINKS": False,
        },
        downloader=disabled_downloader,
    ).crawl()
    assert disabled.items == ({"kind": "start", "processed": True},)
    assert disabled_downloader.urls == ["https://example.test/start"]

    async_start = await Crawler(
        AsyncStartProductSpider,
        {
            "ENGINE_BACKEND": engine,
            "CRAWLSPIDER_FOLLOW_LINKS": False,
        },
        downloader=MappingDownloader(),
    ).crawl()
    assert async_start.items == ({"kind": "start", "processed": True},)

    parse_hook = await Crawler(
        ParseHookSpider,
        {
            "ENGINE_BACKEND": engine,
            "CRAWLSPIDER_FOLLOW_LINKS": False,
        },
        downloader=MappingDownloader(),
    ).crawl()
    assert parse_hook.items == ({"kind": "start", "processed": True, "parse_hook": True},)


async def _verify() -> None:
    _verify_link_extractor()
    for engine in ("python", "rust"):
        await _verify_engine(engine)


if __name__ == "__main__":
    asyncio.run(_verify())
    print(
        "CrawlSpider passed: native extraction, filters, rules, hooks, recursion, "
        "async callbacks, settings, and engine parity"
    )
