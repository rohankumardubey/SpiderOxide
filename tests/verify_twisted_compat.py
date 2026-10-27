from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import threading
from importlib.util import find_spec
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REACTOR_PATH = "twisted.internet.asyncioreactor.AsyncioSelectorReactor"


async def _verify_backend(backend: str) -> None:
    from scrapy.utils.defer import deferred_from_coro
    from scrapy.utils.reactor import (
        install_reactor,
        is_asyncio_reactor_installed,
        is_reactor_installed,
        verify_installed_asyncio_event_loop,
        verify_installed_reactor,
    )
    from twisted.internet import defer, task, threads

    from spideroxide import Crawler, Request, Spider, TextResponse, signals
    from spideroxide.twisted_compat import ASYNCIO_REACTOR
    from spideroxide.utils import maybe_await

    assert REACTOR_PATH == ASYNCIO_REACTOR
    assert not is_reactor_installed()
    try:
        await maybe_await(defer.Deferred())
    except RuntimeError as error:
        assert "TWISTED_REACTOR" in str(error)
    else:
        raise AssertionError("Unconfigured Deferred was accepted")

    events: list[str] = []

    class TwistedHandler:
        lazy = False

        @classmethod
        def from_crawler(cls, crawler: Crawler) -> TwistedHandler:
            instance = cls()
            crawler.signals.connect(instance.spider_opened, signals.spider_opened)
            return instance

        def spider_opened(self, spider: Spider) -> object:
            from twisted.internet import reactor

            return task.deferLater(reactor, 0.01, events.append, "opened")

        def download_request(self, request: Request, spider: Spider) -> object:
            from twisted.internet import reactor

            assert reactor.running is False
            return task.deferLater(
                reactor,
                0.01,
                TextResponse,
                request.url,
                body=b"twisted response",
                request=request,
            )

        def close(self) -> object:
            return threads.deferToThread(events.append, "handler_closed")

    class TwistedMiddleware:
        def process_request(self, request: Request, spider: Spider) -> object:
            return threads.deferToThread(lambda: None)

    class TwistedSpiderMiddleware:
        def process_spider_input(self, response: TextResponse, spider: Spider) -> object:
            return threads.deferToThread(lambda: None)

    class TwistedPipeline:
        def process_item(self, item: dict[str, object], spider: Spider) -> object:
            return threads.deferToThread(lambda: {**item, "pipeline": True})

    class TwistedExtension:
        @classmethod
        def from_crawler(cls, crawler: Crawler) -> TwistedExtension:
            instance = cls()
            crawler.signals.connect(instance.spider_opened, signals.spider_opened)
            crawler.signals.connect(instance.spider_closed, signals.spider_closed)
            return instance

        def spider_opened(self, spider: Spider) -> None:
            self.loop = task.LoopingCall(events.append, "tick")
            self.loop.start(0.01, now=True)

        def spider_closed(self, spider: Spider, reason: str) -> object:
            from twisted.internet import reactor

            self.loop.stop()
            return task.deferLater(reactor, 0.01, events.append, "extension_closed")

    class TwistedSpider(Spider):
        name = "twisted-compat"
        start_urls = ("data:text/html,twisted",)

        async def _parse(self, response: TextResponse) -> dict[str, object]:
            await asyncio.sleep(0.01)
            return {"body": response.text}

        def parse(self, response: TextResponse) -> object:
            return deferred_from_coro(self._parse(response))

    crawler = Crawler(
        TwistedSpider,
        {
            "TWISTED_REACTOR": REACTOR_PATH,
            "ENGINE_BACKEND": backend,
            "DOWNLOAD_HANDLERS": {"data": TwistedHandler},
            "DOWNLOADER_MIDDLEWARES": {TwistedMiddleware: 100},
            "SPIDER_MIDDLEWARES": {TwistedSpiderMiddleware: 100},
            "ITEM_PIPELINES": {TwistedPipeline: 100},
            "EXTENSIONS": {TwistedExtension: 100},
            "ROBOTSTXT_OBEY": False,
        },
    )
    result = await crawler.crawl()
    assert result.items == ({"body": "twisted response", "pipeline": True},), result.items
    assert {"opened", "tick", "extension_closed", "handler_closed"} <= set(events), events
    verify_installed_reactor(REACTOR_PATH)
    assert is_asyncio_reactor_installed()
    assert is_reactor_installed()
    verify_installed_asyncio_event_loop(
        f"{type(asyncio.get_running_loop()).__module__}."
        f"{type(asyncio.get_running_loop()).__qualname__}"
    )
    try:
        verify_installed_asyncio_event_loop("builtins.str")
    except RuntimeError as error:
        assert "does not match" in str(error)
    else:
        raise AssertionError("A mismatched loop class was accepted")

    try:
        install_reactor("twisted.internet.selectreactor.SelectReactor")
    except RuntimeError as error:
        assert "supports only" in str(error)
    else:
        raise AssertionError("Non-asyncio reactor was accepted")

    async def again() -> None:
        try:
            install_reactor(REACTOR_PATH)
        except RuntimeError as error:
            assert "different asyncio event loop" in str(error)
        else:
            raise AssertionError("Reactor was rebound to a second event loop")

    await asyncio.to_thread(lambda: asyncio.run(again()))

    again_result = await Crawler(
        TwistedSpider,
        {"TWISTED_REACTOR": REACTOR_PATH, "ROBOTSTXT_OBEY": False},
    ).crawl()
    assert again_result.items == ({"body": "twisted"},)


async def _verify_conversions() -> None:
    from scrapy.utils.defer import (
        deferred_f_from_coro_f,
        deferred_from_coro,
        deferred_to_future,
        ensure_awaitable,
        maybe_deferred_to_future,
    )
    from twisted.internet import defer

    from spideroxide.twisted_compat import ASYNCIO_REACTOR, start_reactor, stop_reactor
    from spideroxide.utils import maybe_await

    start_reactor(ASYNCIO_REACTOR)
    try:
        from twisted.internet import reactor

        assert await maybe_await(defer.succeed(7)) == 7
        assert await deferred_to_future(defer.succeed(9)) == 9
        assert await maybe_deferred_to_future(defer.succeed(11)) == 11
        assert await ensure_awaitable(defer.succeed(5)) == 5
        assert await ensure_awaitable("ready") == "ready"

        async def compute(value: int) -> int:
            await asyncio.sleep(0.01)
            return value + 1

        assert await maybe_await(deferred_from_coro(compute(12))) == 13
        assert await maybe_await(deferred_f_from_coro_f(compute)(14)) == 15
        try:
            await maybe_await(defer.fail(ValueError("deferred failure")))
        except ValueError as error:
            assert str(error) == "deferred failure"
        else:
            raise AssertionError("Deferred exception was lost")

        pending: defer.Deferred[object] = defer.Deferred()
        waiting = asyncio.create_task(maybe_await(pending))
        await asyncio.sleep(0)
        waiting.cancel()
        try:
            await waiting
        except asyncio.CancelledError:
            assert pending.called
        else:
            raise AssertionError("Cancellation did not reach Twisted")

        timeout: defer.Deferred[object] = defer.Deferred()
        try:
            await asyncio.wait_for(maybe_await(timeout), timeout=0.01)
        except asyncio.TimeoutError:
            assert timeout.called
        else:
            raise AssertionError("Timed-out Deferred was not cancelled")

        timed: defer.Deferred[object] = defer.Deferred()
        timed.addTimeout(0.01, reactor)
        try:
            await maybe_await(timed)
        except defer.TimeoutError:
            assert timed.called
        else:
            raise AssertionError("Twisted Deferred timeout did not propagate")
    finally:
        await stop_reactor()


async def _verify_conflicting_reactor() -> None:
    from twisted.internet import selectreactor

    from spideroxide.twisted_compat import ASYNCIO_REACTOR, start_reactor

    selectreactor.install()
    try:
        start_reactor(ASYNCIO_REACTOR)
    except RuntimeError as error:
        assert "incompatible" in str(error)
    else:
        raise AssertionError("An installed non-asyncio reactor was accepted")


async def _verify_concurrent(backend: str) -> None:
    from twisted.internet import task, threads

    from spideroxide import Crawler, Spider, TextResponse
    from spideroxide.twisted_compat import ASYNCIO_REACTOR

    class ConcurrentSpider(Spider):
        name = "concurrent-twisted"
        start_urls = ("data:text/plain,concurrent",)

        def parse(self, response: TextResponse) -> object:
            from twisted.internet import reactor

            return task.deferLater(reactor, 0.05, lambda: {"value": response.text})

    class ConcurrentPipeline:
        def process_item(self, item: dict[str, str], spider: Spider) -> object:
            return threads.deferToThread(lambda: item)

    settings = {
        "TWISTED_REACTOR": ASYNCIO_REACTOR,
        "ENGINE_BACKEND": backend,
        "ITEM_PIPELINES": {ConcurrentPipeline: 100},
        "ROBOTSTXT_OBEY": False,
    }
    first, second = await asyncio.gather(
        Crawler(ConcurrentSpider, settings).crawl(),
        Crawler(ConcurrentSpider, settings).crawl(),
    )
    assert first.items == second.items == ({"value": "concurrent"},)
    third = await Crawler(
        ConcurrentSpider,
        {key: value for key, value in settings.items() if key != "TWISTED_REACTOR"},
        init_reactor=True,
    ).crawl()
    assert third.items == first.items


async def _verify_failure_cleanup(backend: str) -> None:
    from spideroxide import Crawler, Spider
    from spideroxide.twisted_compat import ASYNCIO_REACTOR

    class BrokenSpider(Spider):
        name = "broken-twisted"

        @classmethod
        def from_crawler(cls, crawler: Crawler) -> BrokenSpider:
            raise ValueError("bootstrap failed")

    try:
        await Crawler(
            BrokenSpider, {"TWISTED_REACTOR": ASYNCIO_REACTOR, "ENGINE_BACKEND": backend}
        ).crawl()
    except ValueError as error:
        assert str(error) == "bootstrap failed"
    else:
        raise AssertionError("Spider construction failure was suppressed")
    from twisted.internet import reactor

    assert reactor.getThreadPool().workers == 0
    assert not reactor.getDelayedCalls()
    from twisted.internet import threads

    from spideroxide.twisted_compat import start_reactor, stop_reactor

    start_reactor(ASYNCIO_REACTOR)
    try:
        assert (
            await threads.deferToThread(lambda: "recovered").asFuture(asyncio.get_running_loop())
            == "recovered"
        )
    finally:
        await stop_reactor()


async def _verify_shutdown_overlap(backend: str) -> None:
    from scrapy.utils.reactor import install_reactor

    from spideroxide import Crawler, Spider
    from spideroxide.twisted_compat import ASYNCIO_REACTOR

    install_reactor(ASYNCIO_REACTOR)
    from twisted.internet import reactor

    entering = threading.Event()
    release = threading.Event()
    original = reactor._stopThreadPool

    def delayed_stop() -> None:
        entering.set()
        if not release.wait(5):
            raise TimeoutError("Twisted shutdown did not resume")
        original()

    reactor._stopThreadPool = delayed_stop

    class PlainSpider(Spider):
        name = "twisted-shutdown-overlap"
        start_urls = ("data:text/plain,overlap",)

        def parse(self, response: object) -> dict[str, bool]:
            return {"done": True}

    settings = {
        "TWISTED_REACTOR": ASYNCIO_REACTOR,
        "ENGINE_BACKEND": backend,
        "ROBOTSTXT_OBEY": False,
    }
    first = asyncio.create_task(Crawler(PlainSpider, settings).crawl())
    try:
        assert await asyncio.to_thread(entering.wait, 5)
        try:
            await Crawler(PlainSpider, settings).crawl()
        except RuntimeError as error:
            assert "shutting down" in str(error)
        else:
            raise AssertionError("A second crawl started while the pool was shutting down")
    finally:
        release.set()
        await first
        reactor._stopThreadPool = original

    result = await Crawler(PlainSpider, settings).crawl()
    assert result.items == ({"done": True},)


async def _verify_crawl_cancellation(backend: str) -> None:
    from twisted.internet.defer import Deferred

    from spideroxide import Crawler, Spider
    from spideroxide.twisted_compat import ASYNCIO_REACTOR

    entered = asyncio.Event()
    cancelled = asyncio.Event()

    class PendingHandler:
        lazy = False

        def download_request(self, request: object, spider: Spider) -> Deferred[object]:
            entered.set()
            return Deferred(lambda deferred: cancelled.set())

    class PendingSpider(Spider):
        name = "cancelled-twisted"
        start_urls = ("data:text/plain,cancel",)

    crawler = Crawler(
        PendingSpider,
        {
            "ENGINE_BACKEND": backend,
            "TWISTED_REACTOR": ASYNCIO_REACTOR,
            "DOWNLOAD_HANDLERS": {"data": PendingHandler},
            "ROBOTSTXT_OBEY": False,
        },
    )
    crawl = asyncio.create_task(crawler.crawl())
    await asyncio.wait_for(entered.wait(), timeout=5)
    crawl.cancel()
    try:
        await crawl
    except asyncio.CancelledError:
        pass
    else:
        raise AssertionError("Cancelled crawl unexpectedly completed")
    assert cancelled.is_set(), "Cancelling a crawl did not cancel its Deferred"


async def _verify_optional(backend: str) -> None:
    from spideroxide import Crawler, Spider

    assert not any(name.startswith("twisted") for name in sys.modules)

    class PlainSpider(Spider):
        name = "without-twisted"
        start_urls = ("data:text/plain,plain",)

        def parse(self, response: object) -> dict[str, bool]:
            return {"ordinary": True}

    result = await Crawler(
        PlainSpider, {"ENGINE_BACKEND": backend, "ROBOTSTXT_OBEY": False}
    ).crawl()
    assert result.items == ({"ordinary": True},)
    assert not any(name.startswith("twisted") for name in sys.modules)


def _run_subprocess(backend: str, mode: str) -> None:
    environment = dict(os.environ)
    environment["PYTHONPATH"] = os.pathsep.join(
        (str(ROOT / "compat" / "scrapy" / "src"), str(ROOT / "src"))
    )
    environment["SPIDEROXIDE_SCRAPY_COMPAT_ALLOW_CONFLICT"] = "1"
    subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), backend, mode],
        cwd=ROOT,
        env=environment,
        check=True,
        timeout=60,
    )


if __name__ == "__main__":
    if len(sys.argv) == 3:
        backend, mode = sys.argv[1:]
        if mode == "crawl":
            asyncio.run(_verify_backend(backend))
        elif mode == "conversions":
            asyncio.run(_verify_conversions())
        elif mode == "conflicting-reactor":
            asyncio.run(_verify_conflicting_reactor())
        elif mode == "concurrent":
            asyncio.run(_verify_concurrent(backend))
        elif mode == "failure-cleanup":
            asyncio.run(_verify_failure_cleanup(backend))
        elif mode == "shutdown-overlap":
            asyncio.run(_verify_shutdown_overlap(backend))
        elif mode == "crawl-cancellation":
            asyncio.run(_verify_crawl_cancellation(backend))
        elif mode == "optional":
            asyncio.run(_verify_optional(backend))
        else:
            raise SystemExit(f"Unknown Twisted verification mode: {mode}")
    elif find_spec("twisted") is None:
        print("Twisted optional dependency not installed; use requirements-twisted.txt")
    else:
        for engine in ("python", "rust"):
            _run_subprocess(engine, "crawl")
            _run_subprocess(engine, "conversions")
            _run_subprocess(engine, "concurrent")
            _run_subprocess(engine, "failure-cleanup")
            _run_subprocess(engine, "shutdown-overlap")
            _run_subprocess(engine, "crawl-cancellation")
            _run_subprocess(engine, "optional")
        _run_subprocess("python", "conflicting-reactor")
        print(
            "Twisted integration passed: Deferred handlers, middleware, callbacks, pipelines, "
            "signals, cancellation, errors, reactor validation, cleanup, and engine parity"
        )
