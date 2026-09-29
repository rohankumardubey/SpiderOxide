from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
COMPAT_SRC = ROOT / "compat" / "scrapy" / "src"
SOURCE = ROOT / "src"
SNAPSHOT = ROOT / "tests" / "_settings_component_snapshot.py"

sys.path.insert(0, str(COMPAT_SRC))
sys.path.insert(1, str(SOURCE))
os.environ["SPIDEROXIDE_SCRAPY_COMPAT_ALLOW_CONFLICT"] = "1"

from scrapy.settings import BaseSettings

from spideroxide import Crawler, Request, Response, Spider
from spideroxide.components import build_components, component_references
from spideroxide.exceptions import NotConfigured


class FirstComponent:
    def __init__(self) -> None:
        self.name = "first"


class DisabledComponent:
    @classmethod
    def from_crawler(cls, crawler: object) -> DisabledComponent:
        assert crawler
        raise NotConfigured("disabled for conformance")


class CrawlerComponent:
    @classmethod
    def from_crawler(cls, crawler: object) -> CrawlerComponent:
        instance = cls()
        instance.crawler = crawler
        return instance


class LastComponent:
    def __init__(self) -> None:
        self.name = "last"


class LifecycleExtension:
    @classmethod
    def from_crawler(cls, crawler: Crawler) -> LifecycleExtension:
        assert crawler.settings["SPIDER_SETTING"] == "spider"
        assert not crawler.settings.frozen
        crawler.settings.set("EXTENSION_SETTING", "applied", "spider")
        instance = cls()
        instance.crawler = crawler
        return instance


class LifecycleDownloaderMiddleware:
    @classmethod
    def from_crawler(cls, crawler: Crawler) -> LifecycleDownloaderMiddleware:
        assert crawler.settings.frozen
        assert crawler.settings["EXTENSION_SETTING"] == "applied"
        instance = cls()
        instance.crawler = crawler
        return instance

    def process_request(self, request: Request, spider: Spider) -> None:
        return None


class LifecycleSpiderMiddleware:
    @classmethod
    def from_crawler(cls, crawler: Crawler) -> LifecycleSpiderMiddleware:
        assert crawler.settings.frozen
        instance = cls()
        instance.crawler = crawler
        return instance

    def process_spider_input(self, response: Response, spider: Spider) -> None:
        return None


class LifecyclePipeline:
    @classmethod
    def from_crawler(cls, crawler: Crawler) -> LifecyclePipeline:
        assert crawler.settings.frozen
        instance = cls()
        instance.crawler = crawler
        return instance

    def process_item(self, item: object, spider: Spider) -> object:
        return item


class LifecycleSpider(Spider):
    name = "settings-lifecycle"
    start_urls = ("data:text/html,%3Ch1%3Esettings%3C%2Fh1%3E",)
    custom_settings = {
        "SPIDER_SETTING": "spider",
        "EXTENSIONS": {LifecycleExtension: 100},
        "DOWNLOADER_MIDDLEWARES": {LifecycleDownloaderMiddleware: 100},
        "SPIDER_MIDDLEWARES": {LifecycleSpiderMiddleware: 100},
        "ITEM_PIPELINES": {LifecyclePipeline: 100},
        "ROBOTSTXT_OBEY": False,
    }

    def parse(self, response: Response) -> dict[str, object]:
        return {
            "extension_setting": self.settings["EXTENSION_SETTING"],
            "status": response.status,
        }


def _snapshot(*, compatibility: bool) -> dict[str, object]:
    environment = dict(os.environ)
    if compatibility:
        environment["PYTHONPATH"] = os.pathsep.join((str(COMPAT_SRC), str(SOURCE), str(ROOT)))
        environment["SPIDEROXIDE_SCRAPY_COMPAT_ALLOW_CONFLICT"] = "1"
    else:
        environment.pop("PYTHONPATH", None)
        environment.pop("SPIDEROXIDE_SCRAPY_COMPAT_ALLOW_CONFLICT", None)
    result = subprocess.run(
        [sys.executable, str(SNAPSHOT)],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _verify_component_collections() -> None:
    crawler = SimpleNamespace(settings=BaseSettings())
    base = {
        FirstComponent: 100,
        DisabledComponent: 200,
        LastComponent: 300,
    }
    custom = {
        FirstComponent: None,
        CrawlerComponent: 250,
        LastComponent: 50,
    }
    components = build_components(custom, crawler, base=base)
    assert [type(component) for component in components] == [
        LastComponent,
        CrawlerComponent,
    ]
    assert components[1].crawler is crawler

    replacement = build_components(
        {
            f"{__name__}.FirstComponent": 200,
        },
        crawler,
        base={
            FirstComponent: 100,
        },
    )
    assert len(replacement) == 1
    assert isinstance(replacement[0], FirstComponent)

    try:
        component_references({FirstComponent: "high"})
    except ValueError as error:
        assert "please provide a real number or None" in str(error)
    else:
        raise AssertionError("non-numeric component priority was accepted")

    try:
        component_references(
            {
                FirstComponent: 100,
                f"{__name__}.FirstComponent": 200,
            }
        )
    except ValueError as error:
        assert "convert to the same object" in str(error)
    else:
        raise AssertionError("duplicate component identity was accepted")


async def _verify_lifecycle() -> None:
    for backend in ("python", "rust"):
        crawler = Crawler(
            LifecycleSpider,
            {
                "ENGINE_BACKEND": backend,
                "EXTENSIONS_BASE": {},
                "DOWNLOADER_MIDDLEWARES_BASE": {},
                "SPIDER_MIDDLEWARES_BASE": {},
            },
        )
        assert crawler.settings["SPIDER_SETTING"] == "spider"
        assert not crawler.settings.frozen
        result = await crawler.crawl()
        assert result.items == (
            {
                "extension_setting": "applied",
                "status": 200,
            },
        )
        assert crawler.settings.frozen
        assert crawler.get_extension(LifecycleExtension)
        assert crawler.get_downloader_middleware(LifecycleDownloaderMiddleware)
        assert crawler.get_spider_middleware(LifecycleSpiderMiddleware)
        assert crawler.get_item_pipeline(LifecyclePipeline)


async def _verify() -> None:
    compatibility = _snapshot(compatibility=True)
    try:
        version("Scrapy")
    except PackageNotFoundError:
        pass
    else:
        upstream = _snapshot(compatibility=False)
        assert compatibility == upstream, json.dumps(
            {
                "compatibility": compatibility,
                "upstream": upstream,
            },
            indent=2,
            sort_keys=True,
        )
    _verify_component_collections()
    await _verify_lifecycle()


if __name__ == "__main__":
    asyncio.run(_verify())
    print(
        "Settings and components passed: priorities, typed access, JSON and module "
        "updates, deletion, freezing, deep copies, class-path loading, factories, "
        "ordering, replacement, disabling, lifecycle timing, inspection, and "
        "NotConfigured behavior"
    )
