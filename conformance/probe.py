from __future__ import annotations

import argparse
import inspect
import json
import platform
import sys
from importlib.metadata import PackageNotFoundError, version
from typing import Any

from itemadapter import ItemAdapter
from scrapy import Field, FormRequest, Item, Request, Selector, Spider
from scrapy.crawler import (
    AsyncCrawlerProcess,
    AsyncCrawlerRunner,
    Crawler,
    CrawlerProcess,
    CrawlerRunner,
)
from scrapy.http import Headers, HtmlResponse, JsonRequest, JsonResponse, Response
from scrapy.linkextractors import LinkExtractor
from scrapy.loader import ItemLoader
from scrapy.settings import SETTINGS_PRIORITIES, Settings
from scrapy.spiderloader import SpiderLoader
from scrapy.utils.python import to_bytes, to_unicode
from scrapy.utils.request import fingerprint, request_from_dict, request_to_curl
from scrapy.utils.response import response_status_message
from scrapy.utils.sitemap import Sitemap, sitemap_urls_from_robots
from scrapy.utils.url import url_is_from_any_domain


def _distribution_version(name: str) -> str | None:
    try:
        return version(name)
    except PackageNotFoundError:
        return None


def _signature(
    target: object,
    *,
    exclude: frozenset[str] = frozenset(),
) -> list[list[str]]:
    return [
        [name, parameter.kind.name]
        for name, parameter in inspect.signature(target).parameters.items()
        if name not in exclude
    ]


def _http_models() -> dict[str, object]:
    headers = Headers(
        {
            "Content-Type": "text/plain",
            "X-Test": ["one", "two"],
        }
    )
    request = Request(
        "https://example.test/path?a=2&a=1",
        method="POST",
        headers=headers,
        body="payload",
        cookies={"session": "value"},
        meta={"depth": 2},
        cb_kwargs={"category": "tools"},
        priority=7,
        dont_filter=True,
        flags=["seed"],
    )
    response = HtmlResponse(
        "https://example.test/catalog/page",
        status=201,
        headers={"X-Response": ["first", "second"]},
        body=b"<html><h1>SpiderOxide</h1><a href='../next'>Next</a></html>",
        encoding="utf-8",
        request=request,
        flags=["cached"],
    )
    form = FormRequest(
        "https://example.test/search",
        formdata=[("q", "rust crawler"), ("tag", "one"), ("tag", "two")],
    )
    json_request = JsonRequest(
        "https://example.test/api",
        data={"name": "SpiderOxide", "count": 2},
    )
    json_response = JsonResponse(
        "https://example.test/api",
        body=b'{"count": 2, "name": "SpiderOxide"}',
        encoding="utf-8",
    )
    replaced = request.replace(
        url="https://example.test/replaced",
        method="PUT",
        priority=11,
    )
    followed = response.follow("../next")
    return {
        "request": {
            "body": request.body.decode(),
            "cb_kwargs": request.cb_kwargs,
            "dont_filter": request.dont_filter,
            "flags": request.flags,
            "header_values": [value.decode() for value in request.headers.getlist("x-test")],
            "meta": request.meta,
            "method": request.method,
            "priority": request.priority,
            "url": request.url,
        },
        "replaced": {
            "body": replaced.body.decode(),
            "method": replaced.method,
            "priority": replaced.priority,
            "url": replaced.url,
        },
        "response": {
            "css": response.css("h1::text").get(),
            "encoding": response.encoding,
            "flags": response.flags,
            "follow": followed.url,
            "header_values": [value.decode() for value in response.headers.getlist("x-response")],
            "status": response.status,
            "urljoin": response.urljoin("../next"),
            "xpath": response.xpath("string(//h1)").get(),
        },
        "form": {
            "body": form.body.decode(),
            "content_type": form.headers["Content-Type"].decode(),
            "method": form.method,
        },
        "json": {
            "body": json.loads(json_request.body),
            "content_type": json_request.headers["Content-Type"].decode(),
            "method": json_request.method,
            "response": json_response.json(),
        },
    }


def _request_identity() -> dict[str, object]:
    requests = [
        Request("https://example.test/path?b=2&a=1"),
        Request("https://example.test/path?a=1&b=2"),
        Request(
            "https://example.test/path?b=2&a=1#fragment",
            method="POST",
            body=b"payload",
            headers={"X-Identity": "one"},
        ),
    ]
    serialized = requests[2].to_dict()
    restored = request_from_dict(serialized)
    return {
        "fingerprints": [fingerprint(request).hex() for request in requests],
        "restored": {
            "body": restored.body.decode(),
            "method": restored.method,
            "url": restored.url,
        },
        "curl": request_to_curl(requests[2]),
        "attributes": list(Request.attributes),
        "canonical_equivalence": fingerprint(requests[0]) == fingerprint(requests[1]),
    }


def _settings() -> dict[str, object]:
    settings = Settings()
    settings.set("VALUE", "default", priority="default")
    settings.set("VALUE", "project", priority="project")
    settings.set("VALUE", "ignored", priority="default")
    settings.set("BOOL_TRUE", "true", priority="command")
    settings.set("LIST_VALUE", "one,two", priority="command")
    copied = settings.copy()
    copied.set("VALUE", "cmdline", priority="cmdline")
    return {
        "bool": settings.getbool("BOOL_TRUE"),
        "copy_independent": copied["VALUE"] != settings["VALUE"],
        "list": settings.getlist("LIST_VALUE"),
        "priorities": SETTINGS_PRIORITIES,
        "priority": settings.getpriority("VALUE"),
        "value": settings["VALUE"],
    }


class Product(Item):
    name = Field()
    tags = Field()


def _selectors_items_links() -> dict[str, object]:
    html = """
    <main>
      <h1> SpiderOxide </h1>
      <a href="/one" class="product">One</a>
      <a href="https://external.test/two">Two</a>
    </main>
    """
    selector = Selector(text=html)
    response = HtmlResponse(
        "https://example.test/catalog/",
        body=html.encode(),
        encoding="utf-8",
    )
    links = LinkExtractor(restrict_css="a.product").extract_links(response)
    loader = ItemLoader(item=Product(), selector=selector)
    loader.add_css("name", "h1::text")
    loader.add_value("tags", ["rust", "crawler"])
    item = loader.load_item()
    return {
        "css": selector.css("a::attr(href)").getall(),
        "xpath": selector.xpath("normalize-space(//h1)").get(),
        "links": [
            {
                "text": link.text,
                "url": link.url,
            }
            for link in links
        ],
        "item": ItemAdapter(item).asdict(),
    }


def _spiders_and_utilities() -> dict[str, object]:
    sitemap = Sitemap(
        b"""<?xml version="1.0" encoding="UTF-8"?>
        <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
          <url><loc>https://example.test/one</loc><lastmod>2026-01-02</lastmod></url>
        </urlset>"""
    )
    return {
        "domain_match": url_is_from_any_domain(
            "https://sub.example.test/path",
            ["example.test"],
        ),
        "robots_sitemaps": list(
            sitemap_urls_from_robots(
                "User-agent: *\nSitemap: https://example.test/sitemap.xml\n",
                base_url="https://example.test/robots.txt",
            )
        ),
        "sitemap": list(sitemap),
        "status": response_status_message(404),
        "to_bytes": to_bytes("SpiderOxide").decode(),
        "to_unicode": to_unicode(b"SpiderOxide"),
    }


def _runtime_api() -> dict[str, object]:
    targets = {
        "AsyncCrawlerProcess": AsyncCrawlerProcess,
        "AsyncCrawlerRunner": AsyncCrawlerRunner,
        "Crawler": Crawler,
        "CrawlerProcess": CrawlerProcess,
        "CrawlerRunner": CrawlerRunner,
        "FormRequest": FormRequest,
        "Headers": Headers,
        "Request": Request,
        "Response": Response,
        "Settings": Settings,
        "Spider": Spider,
        "SpiderLoader": SpiderLoader,
        "fingerprint": fingerprint,
        "request_from_dict": request_from_dict,
        "request_to_curl": request_to_curl,
    }
    return {
        name: _signature(
            target,
            exclude=frozenset({"downloader"}) if name == "Crawler" else frozenset(),
        )
        for name, target in targets.items()
    }


def _real_crawl(backend: str) -> dict[str, object]:
    captured: list[dict[str, object]] = []

    class ConformanceSpider(Spider):
        name = "conformance"
        start_urls = ("data:text/html,%3Chtml%3E%3Ch1%3EConformance%3C%2Fh1%3E%3C%2Fhtml%3E",)

        def parse(self, response: HtmlResponse) -> dict[str, object]:
            item = {
                "status": response.status,
                "title": response.css("h1::text").get(),
                "url": response.url,
            }
            captured.append(item)
            return item

    process = CrawlerProcess(
        {
            "ENGINE_BACKEND": backend,
            "LOG_ENABLED": False,
            "ROBOTSTXT_OBEY": False,
        }
    )
    crawler = process.create_crawler(ConformanceSpider)
    process.crawl(crawler)
    process.start(install_signal_handlers=False)
    stats = crawler.stats.get_stats()
    return {
        "items": captured,
        "stats": {
            key: stats.get(key)
            for key in (
                "downloader/response_count",
                "finish_reason",
                "item_scraped_count",
                "response_received_count",
            )
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--implementation",
        choices=("scrapy", "spideroxide"),
        required=True,
    )
    parser.add_argument("--backend", choices=("python", "rust"), default="python")
    args = parser.parse_args()
    cases = {
        "http-models": _http_models(),
        "request-identity": _request_identity(),
        "runtime-api": _runtime_api(),
        "selectors-items-links": _selectors_items_links(),
        "settings": _settings(),
        "spiders-and-utilities": _spiders_and_utilities(),
        "real-crawl": _real_crawl(args.backend),
    }
    result: dict[str, Any] = {
        "implementation": args.implementation,
        "backend": args.backend,
        "environment": {
            "platform": platform.platform(),
            "python": platform.python_version(),
            "scrapy": _distribution_version("Scrapy"),
            "spideroxide": _distribution_version("spideroxide"),
        },
        "cases": cases,
    }
    json.dump(result, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
