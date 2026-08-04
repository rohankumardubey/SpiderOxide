from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Iterable, Mapping
from typing import TYPE_CHECKING, Any, ClassVar

from .http import Request, Response

if TYPE_CHECKING:
    from .crawler import Crawler
    from .settings import Settings


class Spider:
    name: ClassVar[str]
    start_urls: ClassVar[Iterable[str]] = ()
    custom_settings: ClassVar[Mapping[str, object] | None] = None

    def __init__(self, name: str | None = None, **kwargs: object) -> None:
        spider_name = name or getattr(type(self), "name", None)
        if not spider_name:
            raise ValueError(f"{type(self).__name__} must define a name")
        self.name = spider_name
        for key, value in kwargs.items():
            setattr(self, key, value)
        self.crawler: Crawler | None = None

    @classmethod
    def from_crawler(cls, crawler: Crawler, *args: object, **kwargs: object) -> Spider:
        spider = cls(*args, **kwargs)
        spider.crawler = crawler
        return spider

    @classmethod
    def update_settings(cls, settings: Settings) -> None:
        if cls.custom_settings:
            settings.update_values(cls.custom_settings, priority="spider")

    @property
    def logger(self) -> logging.Logger:
        return logging.getLogger(self.name)

    def start_requests(self) -> Iterable[Request]:
        for url in self.start_urls:
            yield Request(url, callback=self.parse, dont_filter=True)

    async def start(self) -> AsyncIterator[Request]:
        for request in self.start_requests():
            yield request

    def parse(self, response: Response, **kwargs: Any) -> object:
        raise NotImplementedError(f"{type(self).__name__}.parse must be implemented")

    async def closed(self, reason: str) -> None:
        return None

    def __repr__(self) -> str:
        return f"<{type(self).__name__} {self.name!r}>"
