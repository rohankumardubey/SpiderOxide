from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import patch

from parsel.csstranslator import ExpressionError
from w3lib.html import get_base_url

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from spideroxide import (
    Crawler,
    Headers,
    Request,
    Response,
    Selector,
    SelectorList,
    Spider,
    TextResponse,
)

HTML = """
<!doctype html>
<html>
  <head>
    <base href="https://cdn.example.test/assets/">
    <title>SpiderOxide selectors</title>
  </head>
  <body>
    <main id="catalog">
      <article class="product featured" data-id="α-1">
        <h2>Καφές</h2>
        <a href="coffee.html">View <strong>coffee</strong></a>
        <span class="price" data-currency="EUR">12.50</span>
      </article>
      <article class="product" data-id="2">
        <h2>Tea</h2>
        <a href="tea.html">View tea</a>
        <span class="price" data-currency="GBP">8.25</span>
      </article>
      <article class="product"><h2>Malformed
    </main>
    <script type="application/json">{"tags": ["fast", "safe"]}</script>
  </body>
</html>
"""

XML = """<?xml version="1.0"?>
<feed xmlns="urn:example:feed">
  <entry id="1"><title>First</title></entry>
  <entry id="2"><title>Second</title></entry>
</feed>
"""


def _html_response() -> TextResponse:
    return TextResponse(
        "https://example.test/catalog",
        headers=Headers({"Content-Type": "text/html; charset=utf-8"}),
        body=HTML.encode(),
    )


def _verify_response_shortcuts() -> None:
    response = _html_response()
    assert response.selector is response.selector
    assert isinstance(response.selector, Selector)

    products = response.css("article.product")
    assert isinstance(products, SelectorList)
    assert len(products) == 3
    assert products.css("h2::text").getall() == ["Καφές", "Tea", "Malformed\n    "]
    assert products.xpath("./@data-id").getall() == ["α-1", "2"]
    assert response.css("article.featured::attr(data-id)").get() == "α-1"
    assert response.xpath("//article[contains(@class, 'product')]/h2/text()").get() == "Καφές"
    assert response.css("missing::text").get() is None
    assert response.css("missing::text").get(default="not-found") == "not-found"
    assert response.css("article.product").attrib["data-id"] == "α-1"

    first = products[0]
    assert first.css("a::attr(href)").get() == "coffee.html"
    assert first.xpath(".//strong/text()").get() == "coffee"
    assert first.xpath("string(.//a)").get() == "View coffee"
    assert first.css("h2::text").re_first(r"(.+)") == "Καφές"
    assert first.get().startswith('<article class="product featured"')

    script = response.css("script::text")
    assert script.jmespath("tags").getall() == ["fast", "safe"]
    assert response.selector.root.base_url == "https://cdn.example.test/assets/"
    assert response.urljoin("coffee.html") == "https://cdn.example.test/assets/coffee.html"
    assert response.follow("coffee.html").url == "https://cdn.example.test/assets/coffee.html"
    assert response.follow(response.css("a")[0]).url == (
        "https://cdn.example.test/assets/coffee.html"
    )
    assert response.follow(response.css("a::attr(href)")[0]).url == (
        "https://cdn.example.test/assets/coffee.html"
    )

    try:
        response.follow(response.css("a"))
    except ValueError:
        pass
    else:
        raise AssertionError("SelectorList was accepted by follow()")

    replaced = response.replace(body=b"<html><title>Replacement</title></html>")
    assert isinstance(replaced, TextResponse)
    assert replaced.selector is not response.selector
    assert replaced.css("title::text").get() == "Replacement"

    relative_base = TextResponse(
        "https://example.test/catalog/page",
        body=b'<html><base href="../assets/"></html>',
    )
    assert relative_base.selector.root.base_url == "https://example.test/assets/"
    assert relative_base.urljoin("item.html") == "https://example.test/assets/item.html"

    cached_base = TextResponse(
        "https://example.test/catalog",
        body=b'<html><base href="/assets/"></html>',
    )
    with patch("spideroxide.http.get_base_url", wraps=get_base_url) as mocked:
        assert cached_base.urljoin("one") == "https://example.test/assets/one"
        assert cached_base.urljoin("two") == "https://example.test/assets/two"
        mocked.assert_called_once()


def _verify_direct_selectors() -> None:
    selector = Selector(text=HTML, type="html", base_url="https://example.test/")
    assert selector.css("title::text").get() == "SpiderOxide selectors"
    assert selector.xpath("count(//article)").get() == "3.0"
    assert selector.css("a::attr(href)").getall() == ["coffee.html", "tea.html"]
    assert selector.css("article").getall()[2].endswith("</h2></article>")


def _verify_xml_namespaces() -> None:
    response = TextResponse(
        "https://example.test/feed.xml",
        headers=Headers({"Content-Type": "application/atom+xml; charset=utf-8"}),
        body=XML.encode(),
    )
    namespace = {"f": "urn:example:feed"}
    assert response.xpath("//f:entry/@id", namespaces=namespace).getall() == ["1", "2"]
    response.selector.register_namespace("f", "urn:example:feed")
    assert response.xpath("//f:title/text()").getall() == ["First", "Second"]

    utf16 = TextResponse(
        "https://example.test/feed.xml",
        headers=Headers({"Content-Type": "application/xml"}),
        body="<?xml version='1.0'?><root><title>✓</title></root>".encode("utf-16"),
    )
    assert utf16.encoding == "utf-16-le"
    assert utf16.xpath("//title/text()").get() == "✓"

    latin1 = TextResponse(
        "https://example.test/latin1.xml",
        headers=Headers({"Content-Type": "application/xml"}),
        body=b'<?xml version="1.0" encoding="ISO-8859-1"?><root><x>\x80</x></root>',
    )
    assert latin1.encoding == "iso8859-1"
    assert latin1.xpath("//x/text()").get() == "\x80"


def _verify_document_encodings() -> None:
    lazy = TextResponse("https://example.test/large", body=b"x" * (1024 * 1024))
    assert lazy._cached_text is None

    utf16 = TextResponse(
        "https://example.test/utf16",
        body="<html><title>✓</title></html>".encode("utf-16"),
    )
    assert utf16.encoding == "utf-16-le"
    assert not utf16.text.startswith("\ufeff")
    assert utf16.css("title::text").get() == "✓"

    utf8_bom = TextResponse(
        "https://example.test/utf8",
        body=b"\xef\xbb\xbf<html><title>Clean</title></html>",
    )
    assert utf8_bom.text.startswith("<html>")
    assert utf8_bom.css("title::text").get() == "Clean"

    utf16_be = TextResponse(
        "https://example.test/utf16-be",
        headers=Headers({"Content-Type": "text/html; charset=utf-16"}),
        body="<html><title>Big endian</title></html>".encode("utf-16-be"),
    )
    assert utf16_be.encoding == "utf-16-be"
    assert utf16_be.css("title::text").get() == "Big endian"

    utf32_be = TextResponse(
        "https://example.test/utf32-be",
        headers=Headers({"Content-Type": "text/html; charset=utf-32"}),
        body="<html><title>Wide</title></html>".encode("utf-32-be"),
    )
    assert utf32_be.encoding == "utf-32-be"
    assert utf32_be.css("title::text").get() == "Wide"

    latin1 = TextResponse(
        "https://example.test/latin1",
        body=(
            b'<html><meta charset="iso-8859-1"><title>'
            + "café".encode("iso-8859-1")
            + b"</title></html>"
        ),
    )
    assert latin1.encoding == "cp1252"
    assert latin1.css("title::text").get() == "café"

    xhtml = TextResponse(
        "https://example.test/xhtml/page",
        headers=Headers({"Content-Type": "application/xhtml+xml"}),
        body=(
            b'<html><head><base href="../assets/"></head>'
            b'<body><a href="item">Item</a></body></html>'
        ),
    )
    assert xhtml.css("a::text").get() == "Item"
    assert xhtml.urljoin("item") == "https://example.test/assets/item"


def _verify_json_selectors() -> None:
    response = TextResponse(
        "https://example.test/data.json",
        headers=Headers({"Content-Type": "application/problem+json"}),
        body=b'{"products": [{"name": "Coffee"}, {"name": "Tea"}]}',
    )
    assert response.jmespath("products[*].name").getall() == ["Coffee", "Tea"]

    utf8_bom = TextResponse(
        "https://example.test/bom.json",
        headers=Headers({"Content-Type": "application/json"}),
        body=b'\xef\xbb\xbf{"ok": true}',
    )
    assert utf8_bom.jmespath("ok").get() is True

    utf16_bom = TextResponse(
        "https://example.test/utf16.json",
        headers=Headers({"Content-Type": "application/json"}),
        body='{"ok": true}'.encode("utf-16"),
    )
    assert utf16_bom.jmespath("ok").get() is True


def _verify_errors() -> None:
    response = _html_response()
    try:
        response.css("article::unknown")
    except ExpressionError:
        pass
    else:
        raise AssertionError("invalid CSS pseudo-element was accepted")

    try:
        response.xpath("//*[")
    except ValueError:
        pass
    else:
        raise AssertionError("invalid XPath was accepted")


class SelectorDownloader:
    async def fetch(self, request: Request) -> Response:
        return TextResponse(
            request.url,
            headers=Headers({"Content-Type": "text/html; charset=utf-8"}),
            body=HTML.encode(),
            request=request,
        )

    async def close(self) -> None:
        return None


class SelectorSpider(Spider):
    name = "selectors"
    start_urls = ["https://example.test/catalog"]

    def parse(self, response: TextResponse) -> list[dict[str, str | None]]:
        return [
            {
                "name": product.css("h2::text").get(),
                "href": product.css("a::attr(href)").get(),
            }
            for product in response.css("article.product")
        ]


async def _verify_crawler_integration() -> None:
    result = await Crawler(SelectorSpider, downloader=SelectorDownloader()).crawl()
    assert result.items == (
        {"name": "Καφές", "href": "coffee.html"},
        {"name": "Tea", "href": "tea.html"},
        {"name": "Malformed\n    ", "href": None},
    )


def run_selector_checks() -> None:
    _verify_response_shortcuts()
    _verify_direct_selectors()
    _verify_xml_namespaces()
    _verify_document_encodings()
    _verify_json_selectors()
    _verify_errors()
    asyncio.run(_verify_crawler_integration())


if __name__ == "__main__":
    run_selector_checks()
    print("Selectors passed: CSS, XPath, XML namespaces, JSON, regex, chaining, and errors")
