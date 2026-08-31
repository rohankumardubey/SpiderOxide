from __future__ import annotations

import asyncio
import sys
from collections.abc import AsyncIterable, Iterable
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from spideroxide import CloseSpider, Crawler, Request, Response, Spider


class RecordingDownloader:
    def __init__(self) -> None:
        self.urls: list[str] = []
        self.closed = False

    async def fetch(self, request: Request) -> Response:
        self.urls.append(request.url)
        return Response(request.url, request=request)

    async def close(self) -> None:
        self.closed = True


class FailingDownloader(RecordingDownloader):
    async def fetch(self, request: Request) -> Response:
        self.urls.append(request.url)
        raise ValueError("download failure")


class InnerMiddleware:
    @classmethod
    def from_crawler(cls, crawler: Crawler) -> InnerMiddleware:
        middleware = cls()
        middleware.events = crawler.spider.middleware_events
        return middleware

    async def process_start(
        self,
        start: AsyncIterable[object],
        spider: Spider,
    ) -> AsyncIterable[object]:
        self.events.append("start:inner")
        assert spider.name == "middleware"

        async def generate() -> AsyncIterable[object]:
            async for output in start:
                yield output

        return generate()

    def process_spider_input(self, response: Response, spider: Spider) -> None:
        self.events.append(f"input:inner:{response.url.rsplit('/', 1)[-1]}")
        assert spider.name == "middleware"

    def process_spider_output(
        self,
        response: Response,
        result: Iterable[object],
        spider: Spider,
    ) -> Iterable[object]:
        self.events.append(f"output:inner:{response.url.rsplit('/', 1)[-1]}")
        assert spider.name == "middleware"
        for output in result:
            if isinstance(output, dict):
                yield {**output, "inner": True}
            else:
                yield output

    def process_spider_exception(
        self,
        response: Response,
        exception: Exception,
        spider: Spider,
    ) -> Iterable[object] | None:
        self.events.append(f"exception:inner:{response.url.rsplit('/', 1)[-1]}")
        assert spider.name == "middleware"
        if str(exception) == "output failure":
            return [{"recovered": "inner"}]
        return None


class OuterMiddleware:
    @classmethod
    def from_crawler(cls, crawler: Crawler) -> OuterMiddleware:
        middleware = cls()
        middleware.events = crawler.spider.middleware_events
        return middleware

    async def process_start(self, start: AsyncIterable[object]) -> AsyncIterable[object]:
        self.events.append("start:outer")

        async def generate() -> AsyncIterable[object]:
            async for output in start:
                yield output
            yield {"source": "start-middleware"}

        return generate()

    async def process_spider_input(self, response: Response) -> None:
        self.events.append(f"input:outer:{response.url.rsplit('/', 1)[-1]}")

    def process_spider_output(
        self,
        response: Response,
        result: Iterable[object],
    ) -> Iterable[object]:
        raise AssertionError("sync output hook was used instead of async hook")

    async def process_spider_output_async(
        self,
        response: Response,
        result: AsyncIterable[object],
    ) -> AsyncIterable[object]:
        self.events.append(f"output:outer:{response.url.rsplit('/', 1)[-1]}")
        if response.url.endswith("/output-error"):
            yield {"partial": "outer"}
            raise ValueError("output failure")
        async for output in result:
            if isinstance(output, dict):
                yield {**output, "outer": True}
            else:
                yield output

    def process_spider_exception(
        self,
        response: Response,
        exception: Exception,
    ) -> Iterable[object] | None:
        self.events.append(f"exception:outer:{response.url.rsplit('/', 1)[-1]}")
        if str(exception) == "callback failure":
            return [{"recovered": "outer"}]
        return None


class MiddlewareSpider(Spider):
    name = "middleware"
    custom_settings = {
        "CONCURRENT_REQUESTS": 1,
        "RETRY_ENABLED": False,
        "SPIDER_MIDDLEWARES": {
            InnerMiddleware: 100,
            OuterMiddleware: 200,
        },
    }

    def __init__(self) -> None:
        super().__init__()
        self.middleware_events: list[str] = []
        self.errback_calls = 0

    async def start(self):
        for name in ("normal", "callback-error", "output-error"):
            yield Request(
                f"https://example.test/{name}",
                callback=self.parse_page,
                errback=self.handle_error if name == "output-error" else None,
                dont_filter=True,
            )

    def parse_page(self, response: Response) -> dict[str, str]:
        name = response.url.rsplit("/", 1)[-1]
        if name == "callback-error":
            raise ValueError("callback failure")
        return {"source": name}

    def handle_error(self, exception: Exception) -> dict[str, bool]:
        self.errback_calls += 1
        return {"unexpected_errback": True}


class StreamingMiddleware:
    async def process_start(self, start: AsyncIterable[object]) -> AsyncIterable[object]:
        async for output in start:
            yield output


class StreamingSpider(Spider):
    name = "middleware-streaming"
    custom_settings = {
        "CONCURRENT_REQUESTS": 1,
        "SPIDER_MIDDLEWARES": {StreamingMiddleware: 100},
    }

    def __init__(self) -> None:
        super().__init__()
        self.first_processed = asyncio.Event()

    async def start(self):
        yield Request(
            "https://example.test/first",
            callback=self.parse_first,
            dont_filter=True,
        )
        await self.first_processed.wait()
        yield Request(
            "https://example.test/second",
            callback=self.parse_second,
            dont_filter=True,
        )

    def parse_first(self, response: Response) -> dict[str, str]:
        self.first_processed.set()
        return {"source": "first"}

    def parse_second(self, response: Response) -> dict[str, str]:
        return {"source": "second"}


class CallbackStreamingMiddleware:
    @classmethod
    def from_crawler(cls, crawler: Crawler) -> CallbackStreamingMiddleware:
        middleware = cls()
        middleware.spider = crawler.spider
        return middleware

    async def process_spider_output_async(
        self,
        response: Response,
        result: AsyncIterable[object],
    ) -> AsyncIterable[object]:
        async for output in result:
            self.spider.first_output_seen.set()
            yield output


class CallbackStreamingSpider(Spider):
    name = "callback-streaming"
    start_urls = ["https://example.test/callback-streaming"]
    custom_settings = {
        "SPIDER_MIDDLEWARES": {CallbackStreamingMiddleware: 100},
        "RETRY_ENABLED": False,
    }

    def __init__(self) -> None:
        super().__init__()
        self.first_output_seen = asyncio.Event()

    async def parse(self, response: Response) -> AsyncIterable[dict[str, int]]:
        yield {"position": 1}
        await self.first_output_seen.wait()
        yield {"position": 2}


class InvalidOutputMiddleware:
    def process_spider_output(
        self,
        response: Response,
        result: Iterable[object],
    ) -> None:
        return None

    def process_spider_exception(
        self,
        response: Response,
        exception: Exception,
    ) -> list[object]:
        raise AssertionError("invalid middleware output reached exception handlers")


class MaskingMiddleware:
    async def process_spider_output_async(
        self,
        response: Response,
        result: AsyncIterable[object],
    ) -> AsyncIterable[object]:
        try:
            async for output in result:
                yield output
        except Exception:
            yield {"masked": True}


class RaiseAfterInputMiddleware:
    async def process_spider_output_async(
        self,
        response: Response,
        result: AsyncIterable[object],
    ) -> AsyncIterable[object]:
        async for output in result:
            yield output
        raise ValueError("must not replace CloseSpider")


class InvalidSpider(Spider):
    name = "invalid-middleware"
    start_urls = ["https://example.test/invalid"]
    custom_settings = {
        "SPIDER_MIDDLEWARES": {InvalidOutputMiddleware: 100},
        "RETRY_ENABLED": False,
    }

    def parse(self, response: Response) -> dict[str, bool]:
        return {"unexpected": True}


class MaskedInvalidSpider(InvalidSpider):
    name = "masked-invalid-middleware"
    custom_settings = {
        "SPIDER_MIDDLEWARES": {
            MaskingMiddleware: 100,
            InvalidOutputMiddleware: 200,
        },
        "RETRY_ENABLED": False,
    }


class CloseSpiderStreamSpider(Spider):
    name = "close-spider-stream"
    start_urls = ["https://example.test/close"]
    custom_settings = {
        "SPIDER_MIDDLEWARES": {RaiseAfterInputMiddleware: 100},
        "RETRY_ENABLED": False,
    }

    async def parse(self, response: Response) -> AsyncIterable[object]:
        if False:
            yield None
        raise CloseSpider("expected-close")


class InputFailureMiddleware:
    @classmethod
    def from_crawler(cls, crawler: Crawler) -> InputFailureMiddleware:
        middleware = cls()
        middleware.spider = crawler.spider
        return middleware

    def process_spider_input(self, response: Response) -> None:
        raise ValueError("input failure")

    def process_spider_output(
        self,
        response: Response,
        result: Iterable[object],
    ) -> Iterable[object]:
        for output in result:
            if isinstance(output, dict):
                yield {**output, "processed": True}
            else:
                yield output

    def process_spider_exception(
        self,
        response: Response,
        exception: Exception,
    ) -> list[object]:
        self.spider.exception_calls += 1
        return [{"unexpected_exception_recovery": True}]


class InputFailureSpider(Spider):
    name = "input-failure"
    custom_settings = {
        "SPIDER_MIDDLEWARES": {InputFailureMiddleware: 100},
        "RETRY_ENABLED": False,
    }

    def __init__(self) -> None:
        super().__init__()
        self.errback_calls = 0
        self.exception_calls = 0

    async def start(self):
        yield Request(
            "https://example.test/input-failure",
            callback=self.parse,
            errback=self.handle_error,
        )

    def parse(self, response: Response) -> None:
        raise AssertionError("callback ran after spider input failed")

    def handle_error(self, exception: Exception) -> dict[str, bool]:
        assert str(exception) == "input failure"
        self.errback_calls += 1
        return {"errback": True}


class LegacyStartMiddleware:
    @classmethod
    def from_crawler(cls, crawler: Crawler) -> LegacyStartMiddleware:
        middleware = cls()
        middleware.spider = crawler.spider
        return middleware

    def process_start_requests(
        self,
        start_requests: Iterable[object],
        spider: Spider,
    ) -> Iterable[object]:
        self.spider.legacy_calls += 1
        requests = list(start_requests)
        assert len(requests) == 2
        return requests[1:]


class LegacyStartSpider(Spider):
    name = "legacy-start"
    start_urls = [
        "https://example.test/filtered",
        "https://example.test/kept",
    ]
    custom_settings = {
        "SPIDER_MIDDLEWARES": {LegacyStartMiddleware: 100},
        "RETRY_ENABLED": False,
    }

    def __init__(self) -> None:
        super().__init__()
        self.legacy_calls = 0

    def parse(self, response: Response) -> dict[str, str]:
        return {"source": response.url.rsplit("/", 1)[-1]}


class AsyncLegacyStartSpider(LegacyStartSpider):
    name = "async-legacy-start"

    async def start(self):
        yield Request("https://example.test/async", dont_filter=True)


class InvalidModernStartMiddleware:
    async def process_start(self, start: AsyncIterable[object]) -> int:
        return 123


class InvalidModernThenLegacySpider(LegacyStartSpider):
    name = "invalid-modern-then-legacy"
    custom_settings = {
        "SPIDER_MIDDLEWARES": {
            LegacyStartMiddleware: 100,
            InvalidModernStartMiddleware: 200,
        },
        "RETRY_ENABLED": False,
    }


class PartialCallbackMiddleware:
    def process_spider_output(
        self,
        response: Response,
        result: Iterable[object],
    ) -> Iterable[object]:
        for output in result:
            if isinstance(output, dict):
                yield {**output, "processed": True}

    def process_spider_exception(
        self,
        response: Response,
        exception: Exception,
    ) -> Iterable[object] | None:
        if str(exception) == "callback stream failure":
            return [{"recovered": True}]
        return None


class PartialCallbackSpider(Spider):
    name = "partial-callback"
    start_urls = ["https://example.test/partial-callback"]
    custom_settings = {
        "SPIDER_MIDDLEWARES": {PartialCallbackMiddleware: 100},
        "RETRY_ENABLED": False,
    }

    def parse(self, response: Response) -> Iterable[dict[str, bool]]:
        yield {"partial": True}
        raise ValueError("callback stream failure")


class PartialExceptionInnerMiddleware:
    def process_spider_output(
        self,
        response: Response,
        result: Iterable[object],
    ) -> Iterable[object]:
        for output in result:
            if isinstance(output, dict):
                yield {**output, "inner": True}

    def process_spider_exception(
        self,
        response: Response,
        exception: Exception,
    ) -> Iterable[object] | None:
        if str(exception) == "recovery stream failure":
            return [{"recovered": True}]
        return None


class PartialExceptionOuterMiddleware:
    def process_spider_exception(
        self,
        response: Response,
        exception: Exception,
    ) -> Iterable[object] | None:
        if str(exception) != "callback failure":
            return None

        def recover() -> Iterable[dict[str, bool]]:
            yield {"partial_recovery": True}
            raise ValueError("recovery stream failure")

        return recover()


class PartialExceptionSpider(Spider):
    name = "partial-exception"
    start_urls = ["https://example.test/partial-exception"]
    custom_settings = {
        "SPIDER_MIDDLEWARES": {
            PartialExceptionInnerMiddleware: 100,
            PartialExceptionOuterMiddleware: 200,
        },
        "RETRY_ENABLED": False,
    }

    def parse(self, response: Response) -> None:
        raise ValueError("callback failure")


class PartialErrbackMiddleware:
    def process_spider_output(
        self,
        response: Response,
        result: Iterable[object],
    ) -> Iterable[object]:
        for output in result:
            if isinstance(output, dict):
                yield {**output, "processed": True}

    def process_spider_exception(
        self,
        response: Response,
        exception: Exception,
    ) -> Iterable[object] | None:
        if str(exception) == "errback stream failure":
            return [{"recovered": True}]
        return None


class PartialErrbackSpider(Spider):
    name = "partial-errback"
    custom_settings = {
        "SPIDER_MIDDLEWARES": {PartialErrbackMiddleware: 100},
        "RETRY_ENABLED": False,
    }

    async def start(self):
        yield Request(
            "https://example.test/partial-errback",
            callback=self.parse,
            errback=self.handle_error,
        )

    def parse(self, response: Response) -> None:
        raise ValueError("callback failure")

    def handle_error(self, exception: Exception) -> Iterable[dict[str, bool]]:
        assert str(exception) == "callback failure"
        yield {"partial_errback": True}
        raise ValueError("errback stream failure")


class DownloadErrbackSpider(Spider):
    name = "download-errback"

    async def start(self):
        yield Request(
            "https://example.test/download-error",
            errback=self.handle_error,
        )

    def handle_error(self, exception: Exception) -> Iterable[dict[str, bool]]:
        assert str(exception) == "download failure"
        yield {"partial_download_errback": True}
        raise ValueError("download errback stream failure")


class InvalidInputMiddleware:
    def process_spider_input(self, response: Response) -> bool:
        return True


class InvalidInputSpider(Spider):
    name = "invalid-input"
    custom_settings = {
        "SPIDER_MIDDLEWARES": {InvalidInputMiddleware: 100},
        "RETRY_ENABLED": False,
    }

    async def start(self):
        yield Request(
            "https://example.test/invalid-input",
            errback=self.handle_error,
        )

    def handle_error(self, exception: Exception) -> None:
        raise AssertionError("invalid spider input return reached the errback")


async def _verify_engine(engine: str) -> None:
    downloader = RecordingDownloader()
    crawler = Crawler(
        MiddlewareSpider,
        {"ENGINE_BACKEND": engine},
        downloader=downloader,
    )
    result = await crawler.crawl()
    assert downloader.closed is True
    assert len(result.items) == 5
    assert {"source": "normal", "outer": True, "inner": True} in result.items
    assert {"recovered": "outer", "inner": True} in result.items
    assert {"recovered": "inner"} in result.items
    assert {"partial": "outer", "inner": True} in result.items
    assert {"source": "start-middleware"} in result.items

    assert crawler.spider is not None
    assert crawler.spider.errback_calls == 0
    events = crawler.spider.middleware_events
    assert events[:2] == ["start:outer", "start:inner"]
    assert events.index("input:inner:normal") < events.index("input:outer:normal")
    assert events.index("output:outer:normal") < events.index("output:inner:normal")
    assert events.index("exception:outer:callback-error") < events.index(
        "output:inner:callback-error"
    )
    assert "exception:outer:output-error" not in events
    assert events.index("output:outer:output-error") < events.index("output:inner:output-error")
    assert events.index("output:inner:output-error") < events.index("exception:inner:output-error")

    streaming_downloader = RecordingDownloader()
    streaming = await asyncio.wait_for(
        Crawler(
            StreamingSpider,
            {"ENGINE_BACKEND": engine},
            downloader=streaming_downloader,
        ).crawl(),
        timeout=2,
    )
    assert len(streaming.items) == 2
    assert streaming_downloader.urls == [
        "https://example.test/first",
        "https://example.test/second",
    ]

    callback_streaming = await asyncio.wait_for(
        Crawler(
            CallbackStreamingSpider,
            {"ENGINE_BACKEND": engine},
            downloader=RecordingDownloader(),
        ).crawl(),
        timeout=2,
    )
    assert callback_streaming.items == ({"position": 1}, {"position": 2})

    input_failure = Crawler(
        InputFailureSpider,
        {"ENGINE_BACKEND": engine},
        downloader=RecordingDownloader(),
    )
    input_result = await input_failure.crawl()
    assert input_result.items == ({"errback": True, "processed": True},)
    assert input_failure.spider is not None
    assert input_failure.spider.errback_calls == 1
    assert input_failure.spider.exception_calls == 0

    legacy = Crawler(
        LegacyStartSpider,
        {"ENGINE_BACKEND": engine},
        downloader=RecordingDownloader(),
    )
    legacy_result = await legacy.crawl()
    assert legacy_result.items == ({"source": "kept"},)
    assert legacy.spider is not None
    assert legacy.spider.legacy_calls == 1

    partial_callback = await Crawler(
        PartialCallbackSpider,
        {"ENGINE_BACKEND": engine},
        downloader=RecordingDownloader(),
    ).crawl()
    assert partial_callback.items == (
        {"partial": True, "processed": True},
        {"recovered": True},
    )

    partial_exception = await Crawler(
        PartialExceptionSpider,
        {"ENGINE_BACKEND": engine},
        downloader=RecordingDownloader(),
    ).crawl()
    assert partial_exception.items == (
        {"partial_recovery": True, "inner": True},
        {"recovered": True},
    )

    partial_errback = await Crawler(
        PartialErrbackSpider,
        {"ENGINE_BACKEND": engine},
        downloader=RecordingDownloader(),
    ).crawl()
    assert partial_errback.items == (
        {"partial_errback": True, "processed": True},
        {"recovered": True},
    )

    close_result = await Crawler(
        CloseSpiderStreamSpider,
        {"ENGINE_BACKEND": engine},
        downloader=RecordingDownloader(),
    ).crawl()
    assert close_result.reason == "expected-close"

    download_errback = await Crawler(
        DownloadErrbackSpider,
        {"ENGINE_BACKEND": engine},
        downloader=FailingDownloader(),
    ).crawl()
    assert download_errback.items == ({"partial_download_errback": True},)
    assert download_errback.stats["spider_exceptions/count"] == 1


async def _verify_invalid_output(engine: str) -> None:
    crawler = Crawler(
        InvalidSpider,
        {"ENGINE_BACKEND": engine},
        downloader=RecordingDownloader(),
    )
    try:
        await crawler.crawl()
    except TypeError as error:
        assert "process_spider_output must return an iterable" in str(error)
    else:
        raise AssertionError("invalid spider middleware output was accepted")

    for spider, expected in (
        (InvalidInputSpider, "process_spider_input must return None"),
        (MaskedInvalidSpider, "process_spider_output must return an iterable"),
        (
            AsyncLegacyStartSpider,
            "process_start_requests cannot consume asynchronous start output",
        ),
        (InvalidModernThenLegacySpider, "process_start must return an iterable"),
    ):
        crawler = Crawler(
            spider,
            {"ENGINE_BACKEND": engine},
            downloader=RecordingDownloader(),
        )
        try:
            await crawler.crawl()
        except TypeError as error:
            assert expected in str(error)
        else:
            raise AssertionError(f"{spider.name} middleware failure was accepted")


async def _verify() -> None:
    for engine in ("python", "rust"):
        await _verify_engine(engine)
        await _verify_invalid_output(engine)


if __name__ == "__main__":
    asyncio.run(_verify())
    print(
        "Spider middleware passed: start, input, output, exception recovery, "
        "ordering, async streaming, legacy hooks, validation, and engine parity"
    )
