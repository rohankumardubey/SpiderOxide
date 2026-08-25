from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import TYPE_CHECKING

from .http import Request, Response
from .stats import StatsCollector

if TYPE_CHECKING:
    from .crawler import Crawler
    from .spider import Spider

depth_logger = logging.getLogger(__name__)


def runtime_for(crawler: Crawler | None) -> object | None:
    return None if crawler is None else crawler.native_depth_policy


def sync_stats(crawler: Crawler) -> None:
    runtime = runtime_for(crawler)
    if runtime is None:
        return
    for key, delta in runtime.drain_counts().items():
        crawler.stats.inc_value(key, delta)
    max_depth = runtime.drain_max_depth()
    if max_depth is not None:
        crawler.stats.max_value("request_depth_max", int(max_depth))


class DepthMiddleware:
    def __init__(
        self,
        maxdepth: int,
        stats: StatsCollector,
        verbose_stats: bool = False,
        prio: int = 1,
    ) -> None:
        self.maxdepth = maxdepth
        self.stats = stats
        self.verbose_stats = verbose_stats
        self.prio = prio
        self.crawler: Crawler | None = None

    @classmethod
    def from_crawler(cls, crawler: Crawler) -> DepthMiddleware:
        middleware = cls(
            crawler.settings.getint("DEPTH_LIMIT"),
            crawler.stats,
            crawler.settings.getbool("DEPTH_STATS_VERBOSE"),
            crawler.settings.getint("DEPTH_PRIORITY"),
        )
        middleware.crawler = crawler
        return middleware

    def process_spider_output(
        self,
        response: Response,
        result: Iterable[object],
        spider: Spider | None = None,
    ) -> Iterable[object]:
        self._init_depth(response)
        for output in result:
            if not isinstance(output, Request):
                yield output
                continue
            processed = self.get_processed_request(output, response)
            if processed is not None:
                yield processed

    def get_processed_request(
        self,
        request: Request,
        response: Response | None,
    ) -> Request | None:
        if response is None:
            return request
        spider = None if self.crawler is None else self.crawler.spider
        return self._process_request(request, response, spider)

    def _init_depth(self, response: Response) -> None:
        if "depth" in response.meta:
            return
        response.meta["depth"] = 0
        runtime = runtime_for(self.crawler)
        if runtime is not None:
            runtime.record_initial()
            assert self.crawler is not None
            sync_stats(self.crawler)
        elif self.verbose_stats:
            self.stats.inc_value("request_depth_count/0")

    def _process_request(
        self,
        request: Request,
        response: Response,
        spider: Spider | None,
    ) -> Request | None:
        runtime = runtime_for(self.crawler)
        if runtime is not None:
            decision = runtime.process(
                str(response.meta["depth"]),
                str(request.priority),
            )
            assert self.crawler is not None
            sync_stats(self.crawler)
            depth = int(decision.depth)
            priority = int(decision.priority)
            accepted = decision.accepted
        else:
            depth = response.meta["depth"] + 1
            priority = request.priority - depth * self.prio
            accepted = not self.maxdepth or depth <= self.maxdepth
            if accepted:
                if self.verbose_stats:
                    self.stats.inc_value(f"request_depth_count/{depth}")
                self.stats.max_value("request_depth_max", depth)

        if not accepted:
            depth_logger.debug(
                "Ignoring link (depth > %d): %s",
                self.maxdepth,
                request.url,
                extra={"spider": spider},
            )
            return None

        meta = dict(request.meta)
        meta["depth"] = depth
        return request.replace(meta=meta, priority=priority)
