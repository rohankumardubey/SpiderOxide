from __future__ import annotations

import asyncio
import signal
import subprocess
import sys
import tempfile
import warnings
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from spideroxide import (
    AsyncCrawlerProcess,
    AsyncCrawlerRunner,
    Crawler,
    CrawlerProcess,
    CrawlerRunner,
    HtmlResponse,
    Request,
    Response,
    Settings,
    Spider,
    SpiderLoader,
)


class ImmediateDownloader:
    def __init__(self) -> None:
        self.closed = False

    async def fetch(self, request: Request) -> Response:
        return HtmlResponse(
            request.url,
            body=b"<h1>runtime</h1>",
            encoding="utf-8",
            request=request,
        )

    async def close(self) -> None:
        self.closed = True


class BlockingDownloader:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.closed = False

    async def fetch(self, request: Request) -> Response:
        self.started.set()
        await asyncio.Future()
        raise AssertionError("unreachable")

    async def close(self) -> None:
        self.closed = True


class RuntimeExtension:
    pass


class RuntimeDownloaderMiddleware:
    def process_request(self, request: Request, spider: Spider) -> None:
        return None


class RuntimeSpiderMiddleware:
    def process_spider_input(self, response: Response, spider: Spider) -> None:
        return None


class RuntimePipeline:
    def process_item(self, item: object, spider: Spider) -> object:
        return item


class RuntimeSpider(Spider):
    name = "runtime"
    allowed_domains = ("example.test",)
    start_urls = ("https://example.test/",)

    def parse(self, response: HtmlResponse) -> dict[str, object]:
        assert self.settings is self.crawler.settings
        assert self.crawler.get_extension(RuntimeExtension)
        assert self.crawler.get_downloader_middleware(RuntimeDownloaderMiddleware)
        assert self.crawler.get_spider_middleware(RuntimeSpiderMiddleware)
        assert self.crawler.get_item_pipeline(RuntimePipeline)
        return {
            "name": self.name,
            "title": response.css("h1::text").get(),
        }


class NamedRuntimeSpider(RuntimeSpider):
    name = "named-runtime"


class FailingSpider(Spider):
    name = "failing"

    async def start(self):
        raise RuntimeError("startup failed")
        yield


class ConstructionFailingSpider(Spider):
    name = "construction-failing"

    @classmethod
    def from_crawler(cls, crawler: Crawler, *args: object, **kwargs: object):
        raise RuntimeError("construction failed")


class BlockingSpider(Spider):
    name = "blocking"
    start_urls = ("https://example.test/blocking",)

    def parse(self, response: Response) -> None:
        return None


def _settings() -> dict[str, object]:
    return {
        "DOWNLOADER_MIDDLEWARES_BASE": {},
        "DOWNLOADER_MIDDLEWARES": {RuntimeDownloaderMiddleware: 100},
        "SPIDER_MIDDLEWARES_BASE": {},
        "SPIDER_MIDDLEWARES": {RuntimeSpiderMiddleware: 100},
        "ITEM_PIPELINES": {RuntimePipeline: 100},
        "EXTENSIONS_BASE": {},
        "EXTENSIONS": {RuntimeExtension: 100},
        "ROBOTSTXT_OBEY": False,
    }


def _verify_spider_loader() -> None:
    with tempfile.TemporaryDirectory() as directory:
        package = Path(directory) / "runtime_test_spiders"
        nested = package / "nested"
        nested.mkdir(parents=True)
        (package / "__init__.py").write_text("", encoding="utf-8")
        (nested / "__init__.py").write_text("", encoding="utf-8")
        (package / "primary.py").write_text(
            "from spideroxide import Spider\n"
            "class LoadedSpider(Spider):\n"
            "    name = 'loaded-runtime'\n"
            "    allowed_domains = ('example.test',)\n",
            encoding="utf-8",
        )
        (nested / "secondary.py").write_text(
            "from spideroxide import Spider\n"
            "class NestedSpider(Spider):\n"
            "    name = 'nested-runtime'\n",
            encoding="utf-8",
        )
        (package / "duplicate.py").write_text(
            "from spideroxide import Spider\n"
            "class DuplicateSpider(Spider):\n"
            "    name = 'loaded-runtime'\n",
            encoding="utf-8",
        )
        sys.path.insert(0, directory)
        try:
            with warnings.catch_warnings(record=True) as caught:
                loader = SpiderLoader.from_settings(
                    CrawlerRunner(
                        {
                            "SPIDER_MODULES": ["runtime_test_spiders"],
                            "EXTENSIONS_BASE": {},
                        }
                    ).settings
                )
            assert any("same name" in str(warning.message) for warning in caught)
            assert sorted(loader.list()) == ["loaded-runtime", "nested-runtime"]
            assert loader.load("loaded-runtime").name == "loaded-runtime"
            assert sorted(loader.find_by_request(Request("https://example.test/"))) == [
                "loaded-runtime",
                "nested-runtime",
            ]
            assert loader.find_by_request(Request("https://blocked.test/")) == ["nested-runtime"]
            try:
                loader.load("missing")
            except KeyError as error:
                assert error.args == ("Spider not found: missing",)
            else:
                raise AssertionError("missing spider was loaded")
        finally:
            sys.path.remove(directory)
            for module_name in tuple(sys.modules):
                if module_name.startswith("runtime_test_spiders"):
                    sys.modules.pop(module_name)

    strict_settings = Settings(
        {
            "SPIDER_MODULES": ["missing_runtime_spiders"],
            "EXTENSIONS_BASE": {},
        }
    )
    try:
        SpiderLoader.from_settings(strict_settings)
    except ModuleNotFoundError:
        pass
    else:
        raise AssertionError("strict spider loading ignored a missing module")

    warn_settings = Settings(
        {
            "SPIDER_MODULES": ["missing_runtime_spiders"],
            "SPIDER_LOADER_WARN_ONLY": True,
            "EXTENSIONS_BASE": {},
        }
    )
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        assert SpiderLoader.from_settings(warn_settings).list() == []


async def _verify_runner(runner_type: type[AsyncCrawlerRunner]) -> None:
    runner = runner_type(_settings())
    first_downloader = ImmediateDownloader()
    first_task = runner.crawl(RuntimeSpider, downloader=first_downloader)
    assert isinstance(first_task, asyncio.Task)
    assert len(runner.crawlers) == 1
    first = await first_task
    assert first.items == ({"name": "runtime", "title": "runtime"},)
    assert first_downloader.closed
    assert not runner.crawlers

    sequential = await runner.crawl(
        NamedRuntimeSpider,
        downloader=ImmediateDownloader(),
    )
    assert sequential.items == ({"name": "named-runtime", "title": "runtime"},)

    one = runner.crawl(RuntimeSpider, downloader=ImmediateDownloader())
    two = runner.crawl(NamedRuntimeSpider, downloader=ImmediateDownloader())
    assert len(runner.crawlers) == 2
    await runner.join()
    assert one.done() and two.done()
    assert not runner.crawlers

    existing = Crawler(
        RuntimeSpider,
        {
            **_settings(),
            "CONCURRENT_REQUESTS": 3,
        },
        downloader=ImmediateDownloader(),
    )
    assert runner.create_crawler(existing) is existing
    existing_result = await runner.crawl(existing)
    assert existing_result.items[0]["name"] == "runtime"

    try:
        runner.crawl(RuntimeSpider())
    except ValueError as error:
        assert "cannot be a spider object" in str(error)
    else:
        raise AssertionError("runner accepted a spider instance")

    failure = runner.crawl(FailingSpider, downloader=ImmediateDownloader())
    try:
        await failure
    except RuntimeError as error:
        assert str(error) == "startup failed"
    else:
        raise AssertionError("runner swallowed crawl failure")
    assert not runner.bootstrap_failed
    assert not runner.crawlers

    construction_downloader = ImmediateDownloader()
    construction_failure = runner.crawl(
        ConstructionFailingSpider,
        downloader=construction_downloader,
    )
    try:
        await construction_failure
    except RuntimeError as error:
        assert str(error) == "construction failed"
    else:
        raise AssertionError("runner swallowed construction failure")
    assert runner.bootstrap_failed
    assert construction_downloader.closed
    assert not runner.crawlers


async def _verify_stop_and_cancellation() -> None:
    runner = AsyncCrawlerRunner(
        {
            "EXTENSIONS_BASE": {},
            "DOWNLOADER_MIDDLEWARES_BASE": {},
            "SPIDER_MIDDLEWARES_BASE": {},
        }
    )
    blocking = BlockingDownloader()
    task = runner.crawl(BlockingSpider, downloader=blocking)
    await blocking.started.wait()
    await runner.stop()
    result = await task
    assert result.reason == "shutdown"
    assert blocking.closed
    assert not runner.crawlers

    cancelled_downloader = BlockingDownloader()
    cancelled = runner.crawl(
        BlockingSpider,
        downloader=cancelled_downloader,
    )
    await cancelled_downloader.started.wait()
    cancelled.cancel()
    try:
        await cancelled
    except asyncio.CancelledError:
        pass
    else:
        raise AssertionError("cancelled crawl completed successfully")
    await asyncio.sleep(0)
    assert cancelled_downloader.closed
    assert not runner.crawlers


async def _verify_crawler_lifecycle() -> None:
    crawler = Crawler(
        RuntimeSpider,
        _settings(),
        downloader=ImmediateDownloader(),
    )
    result = await crawler.crawl_async()
    assert result is crawler.result
    assert crawler._started
    assert not crawler.crawling
    try:
        await crawler.crawl()
    except RuntimeError as error:
        assert "more than once" in str(error)
    else:
        raise AssertionError("crawler instance was reused")

    pre_engine = Crawler(RuntimeSpider, _settings())
    for method, component_type in (
        (pre_engine.get_extension, RuntimeExtension),
        (pre_engine.get_downloader_middleware, RuntimeDownloaderMiddleware),
        (pre_engine.get_spider_middleware, RuntimeSpiderMiddleware),
        (pre_engine.get_item_pipeline, RuntimePipeline),
    ):
        try:
            method(component_type)
        except RuntimeError:
            pass
        else:
            raise AssertionError("component lookup succeeded before construction")


def _verify_processes() -> None:
    result = subprocess.run(
        [sys.executable, __file__, "--process-child"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "crawler process child passed" in result.stdout


def _process_child() -> None:
    for process_type in (AsyncCrawlerProcess, CrawlerProcess):
        process = process_type(_settings())
        one = process.crawl(RuntimeSpider, downloader=ImmediateDownloader())
        two = process.crawl(NamedRuntimeSpider, downloader=ImmediateDownloader())
        process.start(install_signal_handlers=False)
        assert one.result().items[0]["name"] == "runtime"
        assert two.result().items[0]["name"] == "named-runtime"
        assert process._closed

    graceful = AsyncCrawlerProcess(
        {
            "EXTENSIONS_BASE": {},
            "DOWNLOADER_MIDDLEWARES_BASE": {},
            "SPIDER_MIDDLEWARES_BASE": {},
        }
    )
    blocking = BlockingDownloader()
    stopped = graceful.crawl(BlockingSpider, downloader=blocking)
    graceful._loop.call_later(
        0.05,
        graceful._signal_shutdown,
        signal.SIGTERM,
        None,
    )
    graceful.start(stop_after_crawl=False, install_signal_handlers=False)
    assert stopped.result().reason == "shutdown"
    assert blocking.closed
    assert graceful._closed

    forced = AsyncCrawlerProcess(
        {
            "EXTENSIONS_BASE": {},
            "DOWNLOADER_MIDDLEWARES_BASE": {},
            "SPIDER_MIDDLEWARES_BASE": {},
        }
    )
    interrupted_downloader = BlockingDownloader()
    interrupted = forced.crawl(
        BlockingSpider,
        downloader=interrupted_downloader,
    )
    forced._loop.call_later(
        0.05,
        forced._signal_kill,
        signal.SIGTERM,
        None,
    )
    forced.start(stop_after_crawl=False, install_signal_handlers=False)
    assert interrupted.cancelled()
    assert interrupted_downloader.closed
    assert forced._closed
    print("crawler process child passed")


async def _verify() -> None:
    _verify_spider_loader()
    await _verify_runner(AsyncCrawlerRunner)
    await _verify_runner(CrawlerRunner)
    await _verify_stop_and_cancellation()
    await _verify_crawler_lifecycle()
    _verify_processes()


if __name__ == "__main__":
    if "--process-child" in sys.argv:
        _process_child()
    else:
        asyncio.run(_verify())
        print(
            "Crawler runtime passed: factories, spider loading, sequential and concurrent "
            "crawls, component lookup, failure propagation, graceful stop, cancellation, "
            "standalone process loops, cleanup, and runner compatibility"
        )
