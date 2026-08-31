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
from .depth import DepthMiddleware
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
from .extensions import ExtensionManager
from .feedexport import (
    BaseItemExporter,
    CsvItemExporter,
    FeedExporter,
    FeedSlot,
    FileFeedStorage,
    ItemFilter,
    JsonItemExporter,
    JsonLinesItemExporter,
    StdoutFeedStorage,
    XmlItemExporter,
)
from .headers import Headers
from .http import (
    FormRequest,
    HtmlResponse,
    JsonRequest,
    Request,
    Response,
    TextResponse,
    XmlResponse,
)
from .proxy import HttpProxyMiddleware
from .retry import RetryMiddleware, get_retry_request
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
    "BaseItemExporter",
    "CloseSpider",
    "Crawler",
    "CrawlerRunner",
    "CrawlResult",
    "CsvItemExporter",
    "DownloadError",
    "Downloader",
    "DepthMiddleware",
    "DropItem",
    "DupeFilter",
    "ExtensionManager",
    "FeedExporter",
    "FeedSlot",
    "FingerprintRequest",
    "FileFeedStorage",
    "FormRequest",
    "Headers",
    "HtmlResponse",
    "IgnoreRequest",
    "HttpxDownloader",
    "HttpProxyMiddleware",
    "ItemFilter",
    "JsonItemExporter",
    "JsonLinesItemExporter",
    "JsonRequest",
    "NotConfigured",
    "NativeCrawlEngine",
    "PriorityRequest",
    "Request",
    "RequestData",
    "Response",
    "RetryMiddleware",
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
    "StdoutFeedStorage",
    "TextResponse",
    "XmlResponse",
    "XmlItemExporter",
    "fingerprint",
    "fingerprint_batch",
    "fingerprint_request",
    "fingerprint_requests",
    "get_retry_request",
    "resolve_backend",
    "signals",
]
