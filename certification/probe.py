from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from tempfile import NamedTemporaryFile, TemporaryDirectory
from types import SimpleNamespace

import scrapy
from scrapy import Request

from spideroxide import Crawler, Spider
from spideroxide.settings import Settings
from spideroxide.stats import StatsCollector

ROOT = Path(__file__).resolve().parents[1]
SHIM = ROOT / "compat" / "scrapy" / "src"
if not Path(scrapy.__file__).resolve().is_relative_to(SHIM):
    raise RuntimeError(f"certification imported upstream Scrapy: {scrapy.__file__}")


class ItemSpider(Spider):
    name = "certification"
    start_urls = ("data:text/html,certification",)

    def parse(self, response: object) -> dict[str, object]:
        return {"title": "certified"}


class RepeatSpider(ItemSpider):
    def parse(self, response: object) -> Request:
        return Request(response.url, callback=self.parse_repeat, dont_filter=True)

    def parse_repeat(self, response: object) -> dict[str, object]:
        return {"title": "unexpected duplicate"}


async def deltafetch(backend: str) -> None:
    from scrapy_deltafetch.middleware import DeltaFetch

    with TemporaryDirectory() as directory:
        crawler = Crawler(
            ItemSpider,
            {
                "ENGINE_BACKEND": backend,
                "SPIDER_MIDDLEWARES": {DeltaFetch: 100},
                "DELTAFETCH_ENABLED": True,
                "DELTAFETCH_DIR": directory,
                "ROBOTSTXT_OBEY": False,
            },
        )
        result = await crawler.crawl()
        assert result.items == ({"title": "certified"},), result.items
        assert crawler.get_spider_middleware(DeltaFetch) is not None
        assert crawler.stats.get_value("deltafetch/stored") == 1
        middleware = crawler.get_spider_middleware(DeltaFetch)
        assert middleware is not None
        assert middleware._get_key(Request(ItemSpider.start_urls[0])) is not None

        next_crawler = Crawler(
            RepeatSpider,
            {
                "ENGINE_BACKEND": backend,
                "SPIDER_MIDDLEWARES": {DeltaFetch: 100},
                "DELTAFETCH_ENABLED": True,
                "DELTAFETCH_DIR": directory,
                "ROBOTSTXT_OBEY": False,
            },
        )
        next_result = await next_crawler.crawl()
        assert next_result.items == (), next_result.items
        assert next_crawler.stats.get_value("deltafetch/skipped") == 1


async def spidermon_validation(backend: str) -> None:
    from scrapy.exceptions import DropItem
    from spidermon.contrib.scrapy.pipelines import ItemValidationPipeline

    schema = {
        "type": "object",
        "required": ["title"],
        "properties": {"title": {"type": "string"}},
    }
    crawler = Crawler(
        ItemSpider,
        {
            "ENGINE_BACKEND": backend,
            "ITEM_PIPELINES": {ItemValidationPipeline: 100},
            "SPIDERMON_ENABLED": True,
            "SPIDERMON_VALIDATION_SCHEMAS": {dict: schema},
            "SPIDERMON_VALIDATION_DROP_ITEMS_WITH_ERRORS": True,
            "ROBOTSTXT_OBEY": False,
        },
    )
    result = await crawler.crawl()
    assert result.items == ({"title": "certified"},), result.items
    pipeline = crawler.get_item_pipeline(ItemValidationPipeline)
    assert pipeline is not None
    try:
        pipeline.process_item({"title": 42}, crawler.spider)
    except DropItem:
        pass
    else:
        raise AssertionError("validation pipeline accepted an invalid item")
    assert crawler.stats.get_value("spidermon/validation/items/dropped") == 1


async def redis_dupefilter(backend: str) -> None:
    import fakeredis
    from scrapy_redis.dupefilter import RFPDupeFilter

    server = fakeredis.FakeRedis()
    dedupe = RFPDupeFilter(server, key=f"certification:{backend}")
    first = Request("https://example.test/path?a=1&b=2")
    equivalent = Request("https://example.test/path?b=2&a=1")
    different = Request("https://example.test/path?b=3&a=1")
    assert dedupe.request_seen(first) is False
    assert dedupe.request_seen(equivalent) is True
    assert dedupe.request_seen(different) is False
    assert server.scard(dedupe.key) == 2
    dedupe.close("finished")
    assert server.scard(dedupe.key) == 0


async def splash(backend: str) -> None:
    from scrapy_splash import SplashMiddleware, SplashRequest

    crawler = SimpleNamespace(
        settings=Settings(),
        stats=StatsCollector(),
        signals=SimpleNamespace(connect=lambda *args, **kwargs: None),
    )
    middleware = SplashMiddleware.from_crawler(crawler)
    spider = SimpleNamespace(state={})
    crawler.spider = spider
    request = SplashRequest(
        "https://example.test/page", args={"wait": 0.2}, slot_policy="single_slot"
    )
    middleware.spider_opened(spider)
    rendered = middleware.process_request(request, spider)
    assert rendered.url == "http://127.0.0.1:8050/render.html"
    assert rendered.method == "POST"
    assert json.loads(rendered.body)["url"] == "https://example.test/page"
    assert json.loads(rendered.body)["wait"] == 0.2
    assert crawler.stats.get_value("splash/render.html/request_count") == 1


async def zyte_blocked(backend: str) -> None:
    try:
        from scrapy_zyte_smartproxy.middleware import ScrapyZyteSmartProxyMiddleware
    except ModuleNotFoundError as error:
        assert error.name == "scrapy.resolver", str(error)
    else:
        raise AssertionError(
            f"proxy middleware unexpectedly imports: {ScrapyZyteSmartProxyMiddleware}"
        )


async def xlsx_blocked(backend: str) -> None:
    from scrapy_xlsx import XlsxItemExporter

    with NamedTemporaryFile(suffix=".xlsx") as file:
        exporter = XlsxItemExporter(file)
        try:
            exporter.export_item({"title": "certified"})
        except AttributeError as error:
            assert "_get_serialized_fields" in str(error), str(error)
        else:
            raise AssertionError("exporter unexpectedly succeeded without the removed private API")
        finally:
            exporter.workbook.save(file.name)


async def playwright_blocked(backend: str) -> None:
    try:
        from scrapy_playwright.handler import ScrapyPlaywrightDownloadHandler
    except ModuleNotFoundError as error:
        assert error.name == "scrapy.utils.defer", str(error)
    else:
        raise AssertionError(
            f"browser handler unexpectedly imports: {ScrapyPlaywrightDownloadHandler}"
        )


async def redis_pipeline_blocked(backend: str) -> None:
    import fakeredis
    from scrapy_redis.pipelines import RedisPipeline

    pipeline = RedisPipeline(fakeredis.FakeRedis())
    pending = pipeline.process_item({"title": "certified"}, SimpleNamespace(name="certification"))
    try:
        await asyncio.wait_for(pending, timeout=0.1)
    except RuntimeError as error:
        assert "Task got bad yield" in str(error), str(error)
    else:
        raise AssertionError("Twisted pipeline unexpectedly completed without a reactor")
    finally:
        pending.cancel()


CASES = {
    "deltafetch": deltafetch,
    "spidermon-validation": spidermon_validation,
    "redis-dupefilter": redis_dupefilter,
    "splash": splash,
    "xlsx-blocked": xlsx_blocked,
    "zyte-blocked": zyte_blocked,
    "playwright-blocked": playwright_blocked,
    "redis-pipeline-blocked": redis_pipeline_blocked,
}


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] not in {"python", "rust"} or sys.argv[2] not in CASES:
        raise SystemExit("usage: probe.py {python|rust} CASE")
    asyncio.run(CASES[sys.argv[2]](sys.argv[1]))
