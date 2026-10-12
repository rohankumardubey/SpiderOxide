"""Deprecated crawl-statistics email extension."""

from __future__ import annotations

import asyncio
import warnings
from typing import TYPE_CHECKING

from . import signals
from .exceptions import NotConfigured, ScrapyDeprecationWarning
from .mail import MailSender

if TYPE_CHECKING:
    from .crawler import Crawler
    from .spider import Spider
    from .stats import StatsCollector


class StatsMailer:
    def __init__(self, stats: StatsCollector, recipients: list[str], mail: MailSender) -> None:
        self.stats = stats
        self.recipients = recipients
        self.mail = mail

    @classmethod
    def from_crawler(cls, crawler: Crawler) -> StatsMailer:
        recipients = crawler.settings.getlist("STATSMAILER_RCPTS")
        if not recipients:
            raise NotConfigured
        warnings.warn(
            "The scrapy.extensions.statsmailer module is deprecated and will be "
            "removed in a future release.",
            ScrapyDeprecationWarning,
            stacklevel=2,
        )
        extension = cls(crawler.stats, recipients, MailSender.from_crawler(crawler))
        crawler.signals.connect(extension.spider_closed, signals.spider_closed)
        return extension

    async def spider_closed(self, spider: Spider) -> None:
        spider_stats = self.stats.get_stats()
        body = "Global stats\n\n"
        body += "\n".join(f"{key:<50} : {value}" for key, value in self.stats.get_stats().items())
        body += f"\n\n{spider.name} stats\n\n"
        body += "\n".join(f"{key:<50} : {value}" for key, value in spider_stats.items())
        delivery = self.mail.send(self.recipients, f"Scrapy stats for: {spider.name}", body)
        if isinstance(delivery, asyncio.Task):
            await delivery
