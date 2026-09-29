from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import sys
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMPAT_SRC = ROOT / "compat" / "scrapy" / "src"
SOURCE = ROOT / "src"
SNAPSHOT = ROOT / "tests" / "_addon_snapshot.py"

sys.path.insert(0, str(COMPAT_SRC))
sys.path.insert(1, str(SOURCE))
os.environ["SPIDEROXIDE_SCRAPY_COMPAT_ALLOW_CONFLICT"] = "1"

from scrapy.addons import AddonManager
from scrapy.services import ServiceManager

from spideroxide import (
    AsyncCrawlerRunner,
    Crawler,
    NotConfigured,
    Request,
    Response,
    Spider,
    signals,
)


def _events(crawler: Crawler) -> list[str]:
    events = getattr(crawler, "service_events", None)
    if events is None:
        events = []
        crawler.service_events = events
    return events


class SettingsAddon:
    def __init__(self, crawler: Crawler) -> None:
        self.crawler = crawler

    @classmethod
    def from_crawler(cls, crawler: Crawler) -> SettingsAddon:
        return cls(crawler)

    def update_settings(self, settings: object) -> None:
        settings.set("ADDON_APPLIED", True, "addon")
        settings.setdefault_in_component_priority_dict(
            "SERVICES",
            DatabaseService,
            200,
        )
        settings.setdefault_in_component_priority_dict(
            "SERVICES",
            WorkerService,
            100,
        )


class DisabledAddon:
    def update_settings(self, settings: object) -> None:
        raise NotConfigured


class RunnerAddon:
    @classmethod
    def update_pre_crawler_settings(cls, settings: object) -> None:
        settings.set("SPIDER_MODULES", [], "addon")
        settings.set("PRE_CRAWLER_APPLIED", True, "addon")


class DatabaseService:
    @classmethod
    def from_crawler(cls, crawler: Crawler) -> DatabaseService:
        assert not crawler.settings.frozen
        service = cls()
        service.crawler = crawler
        _events(crawler).append("database:init")
        return service

    def start(self) -> None:
        assert self.crawler.settings.frozen
        assert self.crawler.engine is not None
        _events(self.crawler).append("database:start")

    async def stop(self) -> None:
        await asyncio.sleep(0)
        _events(self.crawler).append("database:stop")


class WorkerService:
    requires = (DatabaseService,)

    @classmethod
    def from_crawler(cls, crawler: Crawler) -> WorkerService:
        assert isinstance(crawler.get_service(DatabaseService), DatabaseService)
        service = cls()
        service.crawler = crawler
        _events(crawler).append("worker:init")
        return service

    async def start(self) -> None:
        await asyncio.sleep(0)
        _events(self.crawler).append("worker:start")

    def stop(self) -> None:
        _events(self.crawler).append("worker:stop")


class DisabledService:
    @classmethod
    def from_crawler(cls, crawler: Crawler) -> DisabledService:
        _events(crawler).append("disabled:init")
        raise NotConfigured


class ServiceObserver:
    @classmethod
    def from_crawler(cls, crawler: Crawler) -> ServiceObserver:
        observer = cls()
        observer.crawler = crawler
        crawler.signals.connect(observer.engine_started, signals.engine_started)
        crawler.signals.connect(observer.engine_stopped, signals.engine_stopped)
        return observer

    def engine_started(self) -> None:
        _events(self.crawler).append("engine:started")

    def engine_stopped(self) -> None:
        _events(self.crawler).append("engine:stopped")


class ServiceSpider(Spider):
    name = "services"
    start_urls = ("https://example.test/services",)
    custom_settings = {
        "ADDONS": {
            DisabledAddon: 10,
            SettingsAddon: 20,
        },
        "SERVICES": {
            DisabledService: 50,
        },
        "EXTENSIONS_BASE": {},
        "EXTENSIONS": {ServiceObserver: 100},
        "DOWNLOADER_MIDDLEWARES_BASE": {},
        "SPIDER_MIDDLEWARES_BASE": {},
        "ROBOTSTXT_OBEY": False,
    }

    def parse(self, response: Response) -> dict[str, object]:
        return {
            "addon": self.settings.getbool("ADDON_APPLIED"),
            "status": response.status,
        }


class BareServiceSpider(Spider):
    name = "bare-services"
    start_urls = ("https://example.test/services",)

    def parse(self, response: Response) -> dict[str, int]:
        return {"status": response.status}


class ServiceDownloader:
    def __init__(self) -> None:
        self.closed = False

    async def fetch(self, request: Request) -> Response:
        return Response(request.url, request=request)

    async def close(self) -> None:
        self.closed = True


class StartFailureService:
    requires = (DatabaseService,)

    @classmethod
    def from_crawler(cls, crawler: Crawler) -> StartFailureService:
        service = cls()
        service.crawler = crawler
        return service

    async def start(self) -> None:
        raise RuntimeError("service startup failed")


class StopFailureService:
    requires = (DatabaseService,)

    @classmethod
    def from_crawler(cls, crawler: Crawler) -> StopFailureService:
        service = cls()
        service.crawler = crawler
        return service

    def start(self) -> None:
        _events(self.crawler).append("stop-failure:start")

    async def stop(self) -> None:
        await asyncio.sleep(0)
        _events(self.crawler).append("stop-failure:stop")
        raise RuntimeError("service shutdown failed")


class MissingDependencyService:
    requires = (DatabaseService,)


class FirstCycleService:
    pass


class SecondCycleService:
    requires = (FirstCycleService,)


FirstCycleService.requires = (SecondCycleService,)


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


def _verify_upstream_addons() -> None:
    compatibility = _snapshot(compatibility=True)
    try:
        version("Scrapy")
    except PackageNotFoundError:
        return
    upstream = _snapshot(compatibility=False)
    assert compatibility == upstream, json.dumps(
        {"compatibility": compatibility, "upstream": upstream},
        indent=2,
        sort_keys=True,
    )


async def _verify_lifecycle(backend: str) -> None:
    downloader = ServiceDownloader()
    crawler = Crawler(
        ServiceSpider,
        {
            "ENGINE_BACKEND": backend,
        },
        downloader=downloader,
    )
    assert isinstance(crawler.addons, AddonManager)
    assert crawler.addons.addons == []
    assert crawler.get_addon(SettingsAddon) is None
    try:
        crawler.get_service(DatabaseService)
    except RuntimeError:
        pass
    else:
        raise AssertionError("service lookup succeeded before service construction")

    result = await crawler.crawl()
    assert result.items == ({"addon": True, "status": 200},)
    assert downloader.closed
    assert isinstance(crawler.services, ServiceManager)
    assert isinstance(crawler.get_addon(SettingsAddon), SettingsAddon)
    assert crawler.get_addon(DisabledAddon) is None
    assert isinstance(crawler.get_service(DatabaseService), DatabaseService)
    assert isinstance(crawler.get_service(WorkerService), WorkerService)
    assert crawler.get_service(DisabledService) is None
    assert [type(service) for service in crawler.services] == [
        DatabaseService,
        WorkerService,
    ]
    assert crawler.service_events == [
        "disabled:init",
        "database:init",
        "worker:init",
        "database:start",
        "worker:start",
        "engine:started",
        "engine:stopped",
        "worker:stop",
        "database:stop",
    ]


async def _verify_start_failure(backend: str) -> None:
    downloader = ServiceDownloader()
    crawler = Crawler(
        BareServiceSpider,
        {
            "ENGINE_BACKEND": backend,
            "SERVICES": {
                DatabaseService: 100,
                StartFailureService: 200,
            },
            "EXTENSIONS_BASE": {},
            "EXTENSIONS": {},
            "DOWNLOADER_MIDDLEWARES_BASE": {},
            "SPIDER_MIDDLEWARES_BASE": {},
        },
        downloader=downloader,
    )
    try:
        await crawler.crawl()
    except RuntimeError as error:
        assert str(error) == "service startup failed"
    else:
        raise AssertionError("crawler swallowed service startup failure")
    assert downloader.closed
    assert crawler.service_events == [
        "database:init",
        "database:start",
        "database:stop",
    ]


async def _verify_stop_failure(backend: str) -> None:
    downloader = ServiceDownloader()
    crawler = Crawler(
        BareServiceSpider,
        {
            "ENGINE_BACKEND": backend,
            "SERVICES": {
                DatabaseService: 100,
                StopFailureService: 200,
            },
            "EXTENSIONS_BASE": {},
            "DOWNLOADER_MIDDLEWARES_BASE": {},
            "SPIDER_MIDDLEWARES_BASE": {},
        },
        downloader=downloader,
    )
    service_logger = logging.getLogger("spideroxide.services")
    logger_disabled = service_logger.disabled
    service_logger.disabled = True
    try:
        try:
            await crawler.crawl()
        except RuntimeError as error:
            assert str(error) == "service shutdown failed"
        else:
            raise AssertionError("crawler swallowed service shutdown failure")
    finally:
        service_logger.disabled = logger_disabled
    assert not crawler.crawling
    assert downloader.closed
    assert crawler.stats.get_value("teardown_errors/count") == 1
    assert crawler.service_events == [
        "database:init",
        "database:start",
        "stop-failure:start",
        "stop-failure:stop",
        "database:stop",
    ]


async def _verify_dependency_errors() -> None:
    for services, expected in (
        ({MissingDependencyService: 100}, "not configured"),
        (
            {
                FirstCycleService: 100,
                SecondCycleService: 200,
            },
            "dependency cycle",
        ),
    ):
        downloader = ServiceDownloader()
        crawler = Crawler(
            BareServiceSpider,
            {
                "SERVICES": services,
                "EXTENSIONS_BASE": {},
                "EXTENSIONS": {},
                "DOWNLOADER_MIDDLEWARES_BASE": {},
                "SPIDER_MIDDLEWARES_BASE": {},
            },
            downloader=downloader,
        )
        try:
            await crawler.crawl()
        except ValueError as error:
            assert expected in str(error)
        else:
            raise AssertionError("invalid service dependencies were accepted")
        assert downloader.closed


def _verify_pre_crawler_settings() -> None:
    runner = AsyncCrawlerRunner(
        {
            "ADDONS": {RunnerAddon: 100},
            "EXTENSIONS_BASE": {},
        }
    )
    assert runner.settings.getbool("PRE_CRAWLER_APPLIED")
    assert runner.settings.getpriority("PRE_CRAWLER_APPLIED") == 15


async def _verify() -> None:
    _verify_upstream_addons()
    _verify_pre_crawler_settings()
    for backend in ("python", "rust"):
        await _verify_lifecycle(backend)
        await _verify_start_failure(backend)
        await _verify_stop_failure(backend)
    await _verify_dependency_errors()


if __name__ == "__main__":
    asyncio.run(_verify())
    print(
        "Add-ons and services passed: upstream ordering, priority mutations, "
        "pre-crawler hooks, factories, opt-outs, dependencies, sync/async lifecycle, "
        "failure cleanup, inspection, and Python/Rust engine parity"
    )
