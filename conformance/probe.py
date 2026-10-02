from __future__ import annotations

import argparse
import importlib
import inspect
import json
import marshal
import pickle
import platform
import sys
from importlib.metadata import PackageNotFoundError, version
from io import BytesIO
from types import SimpleNamespace
from typing import Any
from unittest import TestResult

from itemadapter import ItemAdapter
from scrapy import Field, FormRequest, Item, Request, Selector, Spider
from scrapy.addons import AddonManager
from scrapy.contracts import ContractsManager
from scrapy.contracts.default import (
    CallbackKeywordArgumentsContract,
    ReturnsContract,
    ScrapesContract,
    UrlContract,
)
from scrapy.crawler import (
    AsyncCrawlerProcess,
    AsyncCrawlerRunner,
    Crawler,
    CrawlerProcess,
    CrawlerRunner,
)
from scrapy.exporters import (
    BaseItemExporter,
    MarshalItemExporter,
    PickleItemExporter,
    PprintItemExporter,
    PythonItemExporter,
)
from scrapy.extensions.feedexport import (
    FeedSlot,
    FileFeedStorage,
    FTPFeedStorage,
    StdoutFeedStorage,
    apply_uri_params,
)
from scrapy.http import (
    Headers,
    HtmlResponse,
    JsonRequest,
    JsonResponse,
    Response,
    TextResponse,
)
from scrapy.linkextractors import LinkExtractor
from scrapy.loader import ItemLoader
from scrapy.pipelines.files import FilesPipeline
from scrapy.pipelines.images import ImagesPipeline
from scrapy.settings import SETTINGS_PRIORITIES, BaseSettings, Settings
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
    settings.set("OPTIONAL_NUMBER", None, priority="command")
    return {
        "bool": settings.getbool("BOOL_TRUE"),
        "copy_independent": copied["VALUE"] != settings["VALUE"],
        "float_none": settings.getfloat("OPTIONAL_NUMBER"),
        "int_none": settings.getint("OPTIONAL_NUMBER"),
        "list": settings.getlist("LIST_VALUE"),
        "priorities": SETTINGS_PRIORITIES,
        "priority": settings.getpriority("VALUE"),
        "value": settings["VALUE"],
    }


class FirstAddon:
    def update_settings(self, settings: Settings) -> None:
        settings.set("ADDON_VALUE", "first", priority="addon")
        settings.add_to_list("ADDON_ORDER", "first")


class FactoryAddon:
    @classmethod
    def from_crawler(cls, crawler: object) -> FactoryAddon:
        addon = cls()
        addon.crawler = crawler
        return addon

    def update_settings(self, settings: Settings) -> None:
        settings.set("FACTORY_CRAWLER_MATCH", self.crawler.settings is settings, "addon")
        settings.add_to_list("ADDON_ORDER", "factory")


class LastAddon:
    def update_settings(self, settings: Settings) -> None:
        settings.set("ADDON_VALUE", "last", priority="addon")
        settings.add_to_list("ADDON_ORDER", "last")


class PreCrawlerAddon:
    @classmethod
    def update_pre_crawler_settings(cls, settings: BaseSettings) -> None:
        settings.set("SPIDER_MODULES", ["conformance.spiders"], priority="addon")


def _addons_services() -> dict[str, object]:
    settings = Settings(
        {
            "ADDONS": {
                LastAddon: 30,
                FactoryAddon: 20,
                FirstAddon: 10,
            },
            "ADDON_ORDER": [],
        }
    )
    crawler = SimpleNamespace(settings=settings)
    manager = AddonManager(crawler)
    crawler.addons = manager
    manager.load_settings(settings)

    pre_crawler = BaseSettings({"ADDONS": {PreCrawlerAddon: 10}})
    AddonManager.load_pre_crawler_settings(pre_crawler)
    return {
        "addon_order": settings.getlist("ADDON_ORDER"),
        "addon_types": [type(addon).__name__ for addon in manager.addons],
        "addon_value": settings["ADDON_VALUE"],
        "addon_value_priority": settings.getpriority("ADDON_VALUE"),
        "factory_crawler_match": settings.getbool("FACTORY_CRAWLER_MATCH"),
        "pre_crawler_modules": pre_crawler.getlist("SPIDER_MODULES"),
        "pre_crawler_priority": pre_crawler.getpriority("SPIDER_MODULES"),
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


def _exporters_feed_storage_media() -> dict[str, object]:
    class ExportItem(Item):
        name = Field()
        value = Field()

    item = ExportItem(name="café", value=7)

    pprint_file = BytesIO()
    PprintItemExporter(pprint_file).export_item(item)

    pickle_file = BytesIO()
    PickleItemExporter(pickle_file).export_item(item)

    marshal_file = BytesIO()
    MarshalItemExporter(marshal_file).export_item(item)
    marshal_file.seek(0)

    stdout_file = BytesIO()
    stdout_storage = StdoutFeedStorage("stdout:", _stdout=stdout_file)
    opened_stdout = stdout_storage.open(SimpleNamespace())
    stdout_result = stdout_storage.store(opened_stdout)

    ftp_storage = FTPFeedStorage(
        "ftps://user:p%40ss@example.test:2121/feeds/items.pickle",
        use_active_mode=True,
        feed_options={"overwrite": False},
    )
    file_storage = FileFeedStorage(
        "items.pickle",
        feed_options={"overwrite": True},
    )

    request = Request("https://example.test/assets/file.txt?version=2")
    return {
        "base_signature": _signature(BaseItemExporter),
        "feed_slot_signature": _signature(FeedSlot),
        "file_write_mode": file_storage.write_mode,
        "ftps": {
            "active": ftp_storage.use_active_mode,
            "host": ftp_storage.host,
            "overwrite": ftp_storage.overwrite,
            "password": ftp_storage.password,
            "path": ftp_storage.path,
            "port": ftp_storage.port,
            "tls": ftp_storage.tls,
            "username": ftp_storage.username,
        },
        "marshal": marshal.load(marshal_file),
        "marshal_bytes": marshal_file.getvalue().hex(),
        "media_paths": {
            "file": FilesPipeline.file_path(FilesPipeline.__new__(FilesPipeline), request),
            "image": ImagesPipeline.file_path(ImagesPipeline.__new__(ImagesPipeline), request),
        },
        "pickle": pickle.loads(pickle_file.getvalue()),
        "pickle_bytes": pickle_file.getvalue().hex(),
        "pprint": pprint_file.getvalue().decode(),
        "python": PythonItemExporter().export_item(
            {
                "bytes": b"caf\xc3\xa9",
                "nested": [ExportItem(name=b"tea", value=2)],
            }
        ),
        "stdout": {
            "same_file": opened_stdout is stdout_file,
            "store_result": stdout_result,
            "still_open": not stdout_file.closed,
        },
        "uri": apply_uri_params(
            "file:///tmp/a%20b-%(batch_id)03d-100%%.jl",
            {"batch_id": 7},
        ),
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


def _spider_contracts() -> dict[str, object]:
    class ContractSpider(Spider):
        name = "contract-conformance"

        def parse(self, response: TextResponse, expected: str) -> dict[str, str]:
            """Parse a contract response.

            @url data:text/plain,contract
            @cb_kwargs {"expected": "contract"}
            @returns items 1 1
            @scrapes value
            """
            return {"value": expected}

    manager = ContractsManager(
        (
            UrlContract,
            CallbackKeywordArgumentsContract,
            ReturnsContract,
            ScrapesContract,
        )
    )
    spider = ContractSpider()
    result = TestResult()
    request = manager.from_method(spider.parse, result)
    assert request is not None and request.callback is not None
    response = TextResponse(
        request.url,
        body=b"contract",
        encoding="utf-8",
        request=request,
    )
    request.callback(response, **request.cb_kwargs)
    return {
        "cb_kwargs": request.cb_kwargs,
        "discovery": manager.tested_methods_from_spidercls(ContractSpider),
        "dont_filter": request.dont_filter,
        "errors": len(result.errors),
        "failures": len(result.failures),
        "tests": result.testsRun,
        "url": request.url,
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


def _cli_project_tooling() -> dict[str, object]:
    names = (
        "bench",
        "check",
        "crawl",
        "edit",
        "fetch",
        "genspider",
        "list",
        "parse",
        "runspider",
        "settings",
        "shell",
        "startproject",
        "version",
        "view",
    )
    commands = {}
    for name in names:
        command = importlib.import_module(f"scrapy.commands.{name}").Command()
        commands[name] = {
            "requires_crawler_process": command.requires_crawler_process,
            "requires_project": command.requires_project,
            "short_desc": command.short_desc(),
        }
    return commands


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
        "addons-services": _addons_services(),
        "cli-project-tooling": _cli_project_tooling(),
        "exporters-feed-storage-media": _exporters_feed_storage_media(),
        "http-models": _http_models(),
        "request-identity": _request_identity(),
        "runtime-api": _runtime_api(),
        "selectors-items-links": _selectors_items_links(),
        "settings": _settings(),
        "spider-contracts": _spider_contracts(),
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
