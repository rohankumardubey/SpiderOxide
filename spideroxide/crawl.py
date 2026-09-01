from __future__ import annotations

import copy
from collections.abc import AsyncIterable, AsyncIterator, Callable, Iterable
from typing import Any, ClassVar, cast

from .http import HtmlResponse, Request, Response
from .linkextractors import Link, LinkExtractor
from .spider import Spider
from .utils import _is_output_collection, maybe_await

CallbackReference = Callable[..., object] | str | None
ProcessLinksReference = Callable[[list[Link]], Iterable[Link]] | str | None
ProcessRequestReference = Callable[[Request, Response], Request | None] | str | None


def _identity_process_links(links: list[Link]) -> Iterable[Link]:
    return links


def _identity_process_request(request: Request, response: Response) -> Request:
    return request


def _request_context(function: Callable[..., object]) -> Callable[..., object]:
    function._spideroxide_request_context = True  # type: ignore[attr-defined]
    return function


_DEFAULT_LINK_EXTRACTOR = LinkExtractor()


async def _iterate_outputs(value: object) -> AsyncIterator[object]:
    value = await maybe_await(value)
    if value is None:
        return
    if isinstance(value, AsyncIterable):
        async for output in value:
            yield output
        return
    if _is_output_collection(value):
        for output in value:
            yield output
        return
    yield value


class Rule:
    def __init__(
        self,
        link_extractor: LinkExtractor | None = None,
        callback: CallbackReference = None,
        cb_kwargs: dict[str, Any] | None = None,
        follow: bool | None = None,
        process_links: ProcessLinksReference = None,
        process_request: ProcessRequestReference = None,
        errback: CallbackReference = None,
    ) -> None:
        self.link_extractor = _DEFAULT_LINK_EXTRACTOR if link_extractor is None else link_extractor
        self.callback = callback
        self.errback = errback
        self.cb_kwargs = dict(cb_kwargs or {})
        self.process_links = process_links or _identity_process_links
        self.process_request = process_request or _identity_process_request
        self.follow = not callback if follow is None else bool(follow)

    def _compile(self, spider: CrawlSpider) -> None:
        self.callback = self._resolve(spider, self.callback)
        self.errback = self._resolve(spider, self.errback)
        self.process_links = self._resolve(spider, self.process_links)
        self.process_request = self._resolve(spider, self.process_request)

    @staticmethod
    def _resolve(spider: CrawlSpider, value: object) -> object:
        return getattr(spider, value, None) if isinstance(value, str) else value


class CrawlSpider(Spider):
    rules: ClassVar[Iterable[Rule]] = ()

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)
        self._rules = [copy.copy(rule) for rule in self.rules]
        for rule in self._rules:
            rule._compile(self)

    def start_requests(self) -> Iterable[Request]:
        for url in self.start_urls:
            yield Request(url, callback=self._parse, dont_filter=True)

    def parse_start_url(self, response: Response, **kwargs: Any) -> object:
        return []

    def process_results(
        self,
        response: Response,
        results: object,
    ) -> object:
        return results

    async def _parse(self, response: Response, **kwargs: Any) -> AsyncIterator[object]:
        async for output in self.parse_with_rules(
            response,
            self.parse_start_url,
            kwargs,
            follow=True,
        ):
            yield output

    async def _callback(self, response: Response, **cb_kwargs: Any) -> AsyncIterator[object]:
        rule = self._rules[response.meta["rule"]]
        async for output in self.parse_with_rules(
            response,
            rule.callback,
            {**rule.cb_kwargs, **cb_kwargs},
            follow=rule.follow,
        ):
            yield output

    @_request_context
    def _errback(self, exception: Exception, *, request: Request) -> object:
        rule = self._rules[request.meta["rule"]]
        if rule.errback is None:
            return None
        return cast(Callable[[Exception], object], rule.errback)(exception)

    async def parse_with_rules(
        self,
        response: Response,
        callback: object,
        cb_kwargs: dict[str, Any],
        follow: bool = True,
    ) -> AsyncIterator[object]:
        if callback is not None:
            assert callable(callback)
            callback_output = await maybe_await(callback(response, **cb_kwargs))
            if isinstance(callback_output, AsyncIterable):
                callback_output = [output async for output in callback_output]
            results = self.process_results(
                response,
                () if not callback_output else callback_output,
            )
            async for output in _iterate_outputs(results):
                yield output
        if follow and self._follow_links:
            for request in self._requests_to_follow(response):
                yield request

    @property
    def _follow_links(self) -> bool:
        if self.crawler is None:
            return True
        return self.crawler.settings.getbool("CRAWLSPIDER_FOLLOW_LINKS", True)

    def _requests_to_follow(self, response: Response) -> Iterable[Request]:
        if not isinstance(response, HtmlResponse):
            return
        seen: set[Link] = set()
        for rule_index, rule in enumerate(self._rules):
            links = [
                link for link in rule.link_extractor.extract_links(response) if link not in seen
            ]
            process_links = cast(Callable[[list[Link]], Iterable[Link]], rule.process_links)
            for link in process_links(links):
                seen.add(link)
                request = self._build_request(rule_index, link)
                process_request = cast(
                    Callable[[Request, Response], Request | None],
                    rule.process_request,
                )
                processed = process_request(request, response)
                yield processed

    def _build_request(self, rule_index: int, link: Link) -> Request:
        return Request(
            link.url,
            callback=self._callback,
            errback=self._errback,
            meta={"rule": rule_index, "link_text": link.text},
        )


__all__ = ["CrawlSpider", "Rule"]
