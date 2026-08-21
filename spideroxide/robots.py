from __future__ import annotations

from typing import TYPE_CHECKING

from .backend import BackendUnavailableError
from .exceptions import IgnoreRequest, NotConfigured
from .headers import Headers
from .http import Request, Response

if TYPE_CHECKING:
    from .crawler import Crawler
    from .spider import Spider


def runtime_for(crawler: Crawler | None) -> object | None:
    return None if crawler is None else crawler.native_robots_runtime


def sync_stats(crawler: Crawler) -> None:
    runtime = runtime_for(crawler)
    if runtime is None:
        return
    for name, value in runtime.drain_stats().items():
        crawler.stats.inc_value(name, value)


class RobotsTxtMiddleware:
    download_priority = 1000

    def __init__(self, crawler: Crawler) -> None:
        if not crawler.settings.getbool("ROBOTSTXT_OBEY", False):
            raise NotConfigured
        runtime = runtime_for(crawler)
        if runtime is None:
            raise BackendUnavailableError(
                "ROBOTSTXT_OBEY requires the Rust engine; set ENGINE_BACKEND to 'rust' or 'auto'"
            )
        self.crawler = crawler
        self.runtime = runtime
        self.default_user_agent = str(crawler.settings.get("USER_AGENT", "SpiderOxide/0.1"))
        configured = crawler.settings.get("ROBOTSTXT_USER_AGENT")
        self.robots_user_agent = None if configured in (None, "") else str(configured)

    @classmethod
    def from_crawler(cls, crawler: Crawler) -> RobotsTxtMiddleware:
        return cls(crawler)

    async def process_request(self, request: Request, spider: Spider) -> None:
        if request.meta.get("dont_obey_robotstxt"):
            if not request.meta.get("_robotstxt_request"):
                self.runtime.record_bypass()
                sync_stats(self.crawler)
            return None

        user_agent = self.robots_user_agent
        if user_agent is None:
            header = request.headers.get("User-Agent")
            user_agent = header.decode("latin-1") if header is not None else self.default_user_agent

        while True:
            decision = self.runtime.check(request.url, user_agent)
            if decision.action == "allow":
                sync_stats(self.crawler)
                return None
            if decision.action == "deny":
                sync_stats(self.crawler)
                raise IgnoreRequest("Forbidden by robots.txt")
            if decision.action == "wait":
                if not await self.runtime.wait(decision.origin):
                    raise RuntimeError("native robots policy runtime closed while waiting")
                continue
            if decision.action != "fetch" or decision.robots_url is None:
                raise RuntimeError(f"invalid native robots decision {decision!r}")

            try:
                response = await self._fetch_robots(decision.robots_url)
            except BaseException as error:
                exception_type = f"{type(error).__module__}.{type(error).__qualname__}"
                self.runtime.fail(decision.origin, exception_type)
                sync_stats(self.crawler)
                if isinstance(error, Exception):
                    continue
                raise
            else:
                self.runtime.complete(decision.origin, response.status, response.body)
                sync_stats(self.crawler)

    async def _fetch_robots(self, url: str) -> Response:
        engine = self.crawler.engine
        if engine is None:
            raise RuntimeError("crawler engine is not initialized")
        current = Request(
            url,
            headers=Headers({"User-Agent": self.default_user_agent}),
            meta={
                "dont_obey_robotstxt": True,
                "_robotstxt_request": True,
            },
            priority=self.download_priority,
            dont_filter=True,
        )
        for _ in range(100):
            result = await engine._download(current)
            if isinstance(result, Response):
                return result
            current = result
        raise RuntimeError("robots.txt download exceeded 100 middleware redirects or retries")
