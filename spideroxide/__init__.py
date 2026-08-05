"""Safe, switchable request-processing backends for Python crawlers."""

from . import signals
from .api import (
    DupeFilter,
    Scheduler,
    fingerprint,
    fingerprint_batch,
    fingerprint_request,
    fingerprint_requests,
)
from .backend import (
    BACKEND_ENV_VAR,
    BackendChoice,
    BackendUnavailableError,
    resolve_backend,
)
from .crawler import Crawler, CrawlerRunner
from .downloader import Downloader, HttpxDownloader, RustDownloader
from .engine import CrawlResult, NativeCrawlEngine
from .exceptions import (
    CloseSpider,
    DownloadError,
    DropItem,
    IgnoreRequest,
    NotConfigured,
    SpiderOxideError,
)
from .headers import Headers
from .http import Request, Response, TextResponse
from .selectors import Selector, SelectorList
from .settings import Settings
from .signals import SignalManager
from .spider import Spider
from .stats import StatsCollector
from .types import FingerprintRequest, PriorityRequest, RequestData, ScheduledRequest

__all__ = [
    "BACKEND_ENV_VAR",
    "BackendChoice",
    "BackendUnavailableError",
    "CloseSpider",
    "Crawler",
    "CrawlerRunner",
    "CrawlResult",
    "DownloadError",
    "Downloader",
    "DropItem",
    "DupeFilter",
    "FingerprintRequest",
    "Headers",
    "IgnoreRequest",
    "HttpxDownloader",
    "NotConfigured",
    "NativeCrawlEngine",
    "PriorityRequest",
    "Request",
    "RequestData",
    "Response",
    "RustDownloader",
    "ScheduledRequest",
    "Scheduler",
    "Selector",
    "SelectorList",
    "Settings",
    "SignalManager",
    "Spider",
    "SpiderOxideError",
    "StatsCollector",
    "TextResponse",
    "fingerprint",
    "fingerprint_batch",
    "fingerprint_request",
    "fingerprint_requests",
    "resolve_backend",
    "signals",
]
