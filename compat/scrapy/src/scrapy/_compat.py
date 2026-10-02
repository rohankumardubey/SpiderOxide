from __future__ import annotations

import inspect
import os
import sys
import xmlrpc.client
from collections.abc import AsyncIterable, Iterable, Mapping
from configparser import ConfigParser
from importlib.machinery import ModuleSpec
from pathlib import Path
from types import ModuleType
from typing import Any
from urllib.parse import urlsplit

import spideroxide
from spideroxide import signals as spideroxide_signals
from spideroxide.api import DupeFilter, Scheduler, fingerprint_request
from spideroxide.components import build_from_crawler, load_object
from spideroxide.downloader import _response_type
from spideroxide.exceptions import NotConfigured
from spideroxide.middleware import (
    DownloaderMiddlewareManager,
    ItemPipelineManager,
    SpiderMiddlewareManager,
)
from spideroxide.settings import (
    DEFAULT_SETTINGS,
    PRIORITIES,
    BaseSettings,
    Settings,
    SettingsAttribute,
    get_settings_priority,
)


class ScrapyWarning(Warning):
    pass


class ScrapyDeprecationWarning(ScrapyWarning):
    pass


class UsageError(Exception):
    def __init__(self, *args: object, print_help: bool = True) -> None:
        self.print_help = print_help
        super().__init__(*args)


class ContractFail(AssertionError):
    pass


class NoActiveSpider(Exception):
    pass


class RequestFingerprinter:
    def __init__(self, crawler: object | None = None) -> None:
        self.crawler = crawler

    @classmethod
    def from_crawler(cls, crawler: object) -> RequestFingerprinter:
        return cls(crawler)

    def fingerprint(self, request: object) -> bytes:
        return fingerprint_request(request)


class JsonResponse(spideroxide.TextResponse):
    pass


class XmlRpcRequest(spideroxide.Request):
    def __init__(
        self,
        *args: object,
        encoding: str | None = None,
        **kwargs: object,
    ) -> None:
        if "body" not in kwargs and "params" in kwargs:
            dump_options = {
                key: kwargs.pop(key)
                for key in ("params", "methodname", "methodresponse", "encoding", "allow_none")
                if key in kwargs
            }
            kwargs["body"] = xmlrpc.client.dumps(**dump_options)
        kwargs.setdefault("method", "POST")
        kwargs.setdefault("dont_filter", True)
        if encoding is not None:
            kwargs["encoding"] = encoding
        super().__init__(*args, **kwargs)
        self.headers.setdefault("Content-Type", "text/xml")


class BaseDupeFilter(DupeFilter):
    @classmethod
    def from_settings(cls, settings: Settings) -> BaseDupeFilter:
        return cls()

    @classmethod
    def from_crawler(cls, crawler: object) -> BaseDupeFilter:
        return cls()

    def request_seen(self, request: object) -> bool:
        return self.seen_request(request)

    def open(self) -> None:
        return None

    def close(self, reason: str) -> None:
        return None

    def log(self, request: object, spider: object) -> None:
        return None


RFPDupeFilter = BaseDupeFilter


class _QueueMarker:
    pass


class FifoMemoryQueue(_QueueMarker):
    pass


class LifoMemoryQueue(_QueueMarker):
    pass


class PickleFifoDiskQueue(_QueueMarker):
    pass


class PickleLifoDiskQueue(_QueueMarker):
    pass


class MarshalFifoDiskQueue(_QueueMarker):
    pass


class MarshalLifoDiskQueue(_QueueMarker):
    pass


def global_object_name(value: object) -> str:
    return f"{value.__module__}.{value.__qualname__}"  # type: ignore[attr-defined]


def to_bytes(
    value: str | bytes,
    encoding: str | None = None,
    errors: str = "strict",
) -> bytes:
    if isinstance(value, bytes):
        return value
    if not isinstance(value, str):
        raise TypeError(f"to_bytes must receive str or bytes, got {type(value).__name__}")
    return value.encode(encoding or "utf-8", errors)


def to_unicode(
    value: str | bytes,
    encoding: str | None = None,
    errors: str = "strict",
) -> str:
    if isinstance(value, str):
        return value
    if not isinstance(value, bytes):
        raise TypeError(f"to_unicode must receive str or bytes, got {type(value).__name__}")
    return value.decode(encoding or "utf-8", errors)


def without_none_values(mapping: Mapping[Any, Any]) -> dict[Any, Any]:
    return {key: value for key, value in mapping.items() if value is not None}


def arg_to_iter(value: object) -> Iterable[object]:
    if value is None:
        return ()
    if isinstance(value, (str, bytes, Mapping)):
        return (value,)
    if isinstance(value, Iterable):
        return value
    return (value,)


def closest_scrapy_cfg(path: str | os.PathLike[str] = ".") -> str:
    current = Path(path).resolve()
    if current.is_file():
        current = current.parent
    for directory in (current, *current.parents):
        candidate = directory / "scrapy.cfg"
        if candidate.is_file():
            return str(candidate)
    return ""


def inside_project() -> bool:
    module_name = os.environ.get("SCRAPY_SETTINGS_MODULE")
    if module_name:
        try:
            __import__(module_name)
        except ImportError:
            pass
        else:
            return True
    return bool(closest_scrapy_cfg())


def find_projects(
    path: str | os.PathLike[str] = ".",
    *,
    max_depth: int | None = None,
    ignored_dirs: Iterable[str] = (),
) -> Iterable[Path]:
    root = Path(path)
    ignored = frozenset(ignored_dirs)
    for directory, directories, files in os.walk(root):
        current = Path(directory)
        if "scrapy.cfg" in files:
            directories.clear()
            yield current
            continue
        if "pyvenv.cfg" in files:
            directories.clear()
            continue
        if max_depth is not None and len(current.relative_to(root).parts) >= max_depth:
            directories.clear()
            continue
        directories[:] = sorted(
            name for name in directories if not name.startswith(".") and name not in ignored
        )


def project_data_dir(project: str = "default") -> str:
    config_path = closest_scrapy_cfg()
    if not config_path:
        raise NotConfigured("Not inside a project")
    config = ConfigParser()
    config.read(config_path)
    if config.has_option("datadir", project):
        directory = Path(config.get("datadir", project))
    else:
        directory = Path(config_path).parent / ".scrapy"
    directory.mkdir(parents=True, exist_ok=True)
    return str(directory.resolve())


def data_path(path: str | os.PathLike[str], createdir: bool = False) -> str:
    resolved = Path(path)
    if not resolved.is_absolute():
        resolved = (
            Path(project_data_dir()) / resolved if inside_project() else Path(".scrapy") / resolved
        )
    if createdir:
        resolved.mkdir(parents=True, exist_ok=True)
    return str(resolved)


def get_project_settings() -> Settings:
    settings = Settings()
    module_name = os.environ.get("SCRAPY_SETTINGS_MODULE")
    config_path = closest_scrapy_cfg()
    if module_name is None and config_path:
        project_root = str(Path(config_path).parent)
        if project_root not in sys.path:
            sys.path.insert(0, project_root)
        config = ConfigParser()
        config.read(config_path)
        project = os.environ.get("SCRAPY_PROJECT", "default")
        module_name = config.get("settings", project, fallback=None)
        if module_name:
            os.environ["SCRAPY_SETTINGS_MODULE"] = module_name
    if module_name:
        settings.setmodule(module_name, priority="project")
    valid_envvars = {
        "CHECK",
        "PROJECT",
        "PYTHON_SHELL",
        "SETTINGS_MODULE",
    }
    settings.setdict(
        {
            name.removeprefix("SCRAPY_"): value
            for name, value in os.environ.items()
            if name.startswith("SCRAPY_") and name.removeprefix("SCRAPY_") in valid_envvars
        },
        priority="project",
    )
    return settings


def request_from_dict(d: Mapping[str, object], *, spider: object | None = None) -> object:
    data = dict(d)
    class_path = data.pop("_class", None)
    request_type = spideroxide.Request if class_path is None else load_object(str(class_path))
    for field in ("callback", "errback"):
        value = data.get(field)
        if isinstance(value, str):
            if spider is None:
                raise ValueError(f"request {field} requires a spider instance")
            data[field] = getattr(spider, value)
    return request_type(**data)


def fingerprint(
    request: object,
    *,
    include_headers: Iterable[str | bytes] | None = None,
    keep_fragments: bool = False,
) -> bytes:
    return fingerprint_request(
        request,
        include_headers=include_headers,
        keep_fragments=keep_fragments,
    )


def request_to_curl(request: object) -> str:
    data = f"--data-raw '{request.body.decode('utf-8')}'" if request.body else ""
    headers = " ".join(
        f"-H '{name.decode()}: {values[0].decode()}'"
        for name, values in request.headers.to_scrapy_dict().items()
    )
    if isinstance(request.cookies, Mapping):
        cookie_values = request.cookies.items()
    else:
        cookie_values = (
            (cookie["name"], cookie["value"])
            for cookie in request.cookies
            if "name" in cookie and "value" in cookie
        )
    pairs = [
        f"{to_unicode(name if isinstance(name, (str, bytes)) else str(name), request.encoding)}="
        f"{to_unicode(value if isinstance(value, (str, bytes)) else str(value), request.encoding)}"
        for name, value in cookie_values
    ]
    cookies = f"--cookie '{'; '.join(pairs)}'" if pairs else ""
    curl_command = f"curl -X {request.method} {request.url} {data} {headers} {cookies}".strip()
    return " ".join(curl_command.split())


def referer_str(request: object) -> str | None:
    value = request.headers.get("Referer")
    return None if value is None else value.decode("latin-1")


def url_is_from_any_domain(url: str, domains: Iterable[str]) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    return any(host == domain.lower() or host.endswith(f".{domain.lower()}") for domain in domains)


def response_status_message(status: int) -> str:
    from http import HTTPStatus

    try:
        return f"{status} {HTTPStatus(status).phrase}"
    except ValueError:
        return str(status)


def iterate_spider_output(result: object) -> object:
    if inspect.isawaitable(result) or isinstance(result, AsyncIterable):
        return result
    if result is None:
        return ()
    if isinstance(result, (spideroxide.Request, Mapping)):
        return (result,)
    if isinstance(result, Iterable) and not isinstance(result, (str, bytes)):
        return result
    return (result,)


class ResponseTypes:
    def from_args(
        self,
        headers: object | None = None,
        url: str | None = None,
        filename: str | None = None,
        body: bytes | None = None,
    ) -> type:
        del filename
        return _response_type(headers or {}, url=url or "", body=body or b"")


responsetypes = ResponseTypes()


def _module(name: str, attributes: Mapping[str, object], *, package: bool = False) -> ModuleType:
    current = sys.modules.get(name)
    if current is not None:
        current.__dict__.update(attributes)
        return current
    module = ModuleType(name)
    module.__dict__.update(attributes)
    module.__package__ = name if package else name.rpartition(".")[0]
    module.__spec__ = ModuleSpec(name, loader=None, is_package=package)
    if package:
        module.__path__ = []  # type: ignore[attr-defined]
    module.__all__ = sorted(key for key in attributes if not key.startswith("_"))
    sys.modules[name] = module
    parent_name, _, child_name = name.rpartition(".")
    parent = sys.modules.get(parent_name)
    if parent is not None:
        setattr(parent, child_name, module)
    return module


def _public(module: ModuleType) -> dict[str, object]:
    return {name: value for name, value in vars(module).items() if not name.startswith("_")}


def install() -> None:
    if "scrapy.http" in sys.modules:
        return

    from itemloaders import processors

    from spideroxide import (
        AddonManager,
        AsyncCrawlerProcess,
        AsyncCrawlerRunner,
        BaseDownloadHandler,
        BaseItemExporter,
        BlockingFeedStorage,
        Bz2Plugin,
        CloseSpider,
        CookiesMiddleware,
        CoreStats,
        Crawler,
        CrawlerProcess,
        CrawlerRunner,
        CrawlSpider,
        CSVFeedSpider,
        CsvItemExporter,
        DataURIDownloadHandler,
        DefaultHeadersMiddleware,
        DefaultReferrerPolicy,
        DepthMiddleware,
        DontCloseSpider,
        DownloadError,
        DownloaderStatsMiddleware,
        DownloadHandlers,
        DownloadTimeoutMiddleware,
        DropItem,
        DummyPolicy,
        FeedExporter,
        FeedSlot,
        Field,
        FileDownloadHandler,
        FileFeedStorage,
        FileInfo,
        FilesPipeline,
        FormRequest,
        FTPDownloadHandler,
        FTPFeedStorage,
        FTPFilesStore,
        GCSFeedStorage,
        GCSFilesStore,
        GzipPlugin,
        Headers,
        HtmlResponse,
        HttpAuthMiddleware,
        HttpCacheMiddleware,
        HttpCompressionMiddleware,
        HTTPDownloadHandler,
        HttpError,
        HttpErrorMiddleware,
        HttpProxyMiddleware,
        IgnoreRequest,
        ImageException,
        ImagesPipeline,
        Item,
        ItemFilter,
        ItemLoader,
        JsonItemExporter,
        JsonLinesItemExporter,
        JsonRequest,
        Link,
        LinkExtractor,
        LogCount,
        LogStats,
        LZMAPlugin,
        MarshalItemExporter,
        MediaPipeline,
        MemoryDebugger,
        MemoryUsage,
        MetaCopyDetectionMiddleware,
        MetaRefreshMiddleware,
        NativeHttpCacheStorage,
        NoReferrerPolicy,
        NoReferrerWhenDowngradePolicy,
        NotConfigured,
        NotSupported,
        OffsiteMiddleware,
        OriginPolicy,
        OriginWhenCrossOriginPolicy,
        PickleItemExporter,
        PostProcessingManager,
        PprintItemExporter,
        PythonItemExporter,
        RedirectMiddleware,
        RefererMiddleware,
        ReferrerPolicy,
        Request,
        Response,
        RetryMiddleware,
        RFC2616Policy,
        Rule,
        S3DownloadHandler,
        S3FeedStorage,
        S3FilesStore,
        SameOriginPolicy,
        Selector,
        SelectorList,
        ServiceManager,
        SignalManager,
        Sitemap,
        SitemapSpider,
        Spider,
        SpiderLoader,
        SpiderState,
        StartSpiderMiddleware,
        StatsCollector,
        StdoutFeedStorage,
        StopDownload,
        StrictOriginPolicy,
        StrictOriginWhenCrossOriginPolicy,
        TextResponse,
        UnsafeUrlPolicy,
        UrlLengthMiddleware,
        UserAgentMiddleware,
        XMLFeedSpider,
        XmlItemExporter,
        XmlResponse,
        apply_uri_params,
        get_retry_request,
        sitemap_urls_from_robots,
    )
    from spideroxide.extensions import ExtensionManager
    from spideroxide.iterators import csviter, xmliter_lxml
    from spideroxide.linkextractors import IGNORED_EXTENSIONS
    from spideroxide.operational import CloseSpider as CloseSpiderExtension
    from spideroxide.pipelines import FSFilesStore
    from spideroxide.robots import RobotsTxtMiddleware
    from spideroxide.trackref import live_refs, object_ref
    from spideroxide.types import ItemMeta

    http = {
        "FormRequest": FormRequest,
        "Headers": Headers,
        "HtmlResponse": HtmlResponse,
        "JsonRequest": JsonRequest,
        "JsonResponse": JsonResponse,
        "Request": Request,
        "Response": Response,
        "TextResponse": TextResponse,
        "XmlResponse": XmlResponse,
        "XmlRpcRequest": XmlRpcRequest,
    }
    _module("scrapy.http", http, package=True)
    _module("scrapy.http.headers", {"Headers": Headers})
    _module("scrapy.http.request", {"Request": Request}, package=True)
    _module("scrapy.http.request.form", {"FormRequest": FormRequest})
    _module("scrapy.http.request.json_request", {"JsonRequest": JsonRequest})
    _module("scrapy.http.request.rpc", {"XmlRpcRequest": XmlRpcRequest})
    _module(
        "scrapy.http.response",
        {
            "Response": Response,
            "TextResponse": TextResponse,
            "HtmlResponse": HtmlResponse,
            "JsonResponse": JsonResponse,
            "XmlResponse": XmlResponse,
        },
        package=True,
    )
    _module("scrapy.http.response.text", {"TextResponse": TextResponse})
    _module("scrapy.http.response.html", {"HtmlResponse": HtmlResponse})
    _module("scrapy.http.response.json", {"JsonResponse": JsonResponse})
    _module("scrapy.http.response.xml", {"XmlResponse": XmlResponse})

    _module("scrapy.item", {"Field": Field, "Item": Item, "ItemMeta": ItemMeta})
    _module("scrapy.selector", {"Selector": Selector, "SelectorList": SelectorList})
    _module("scrapy.loader", {"ItemLoader": ItemLoader}, package=True)
    _module("scrapy.loader.processors", _public(processors))
    _module("scrapy.link", {"Link": Link})
    _module(
        "scrapy.linkextractors",
        {
            "IGNORED_EXTENSIONS": list(IGNORED_EXTENSIONS),
            "LinkExtractor": LinkExtractor,
            "LxmlLinkExtractor": LinkExtractor,
        },
        package=True,
    )
    _module(
        "scrapy.linkextractors.lxmlhtml",
        {"LxmlLinkExtractor": LinkExtractor},
    )
    _module(
        "scrapy.spiders",
        {
            "CSVFeedSpider": CSVFeedSpider,
            "CrawlSpider": CrawlSpider,
            "Rule": Rule,
            "SitemapSpider": SitemapSpider,
            "Spider": Spider,
            "XMLFeedSpider": XMLFeedSpider,
        },
        package=True,
    )
    _module("scrapy.spiders.crawl", {"CrawlSpider": CrawlSpider, "Rule": Rule})
    _module(
        "scrapy.spiders.feed",
        {
            "CSVFeedSpider": CSVFeedSpider,
            "XMLFeedSpider": XMLFeedSpider,
        },
    )
    _module("scrapy.spiders.sitemap", {"SitemapSpider": SitemapSpider})

    _module(
        "scrapy.settings",
        {
            "BaseSettings": BaseSettings,
            "Settings": Settings,
            "SettingsAttribute": SettingsAttribute,
            "SETTINGS_PRIORITIES": PRIORITIES,
            "get_settings_priority": get_settings_priority,
        },
        package=True,
    )
    _module(
        "scrapy.settings.default_settings",
        {
            **DEFAULT_SETTINGS,
            "SETTINGS_PRIORITIES": PRIORITIES,
        },
    )
    _module(
        "scrapy.crawler",
        {
            "AsyncCrawlerProcess": AsyncCrawlerProcess,
            "AsyncCrawlerRunner": AsyncCrawlerRunner,
            "Crawler": Crawler,
            "CrawlerProcess": CrawlerProcess,
            "CrawlerRunner": CrawlerRunner,
        },
    )
    _module("scrapy.addons", {"AddonManager": AddonManager})
    _module("scrapy.services", {"ServiceManager": ServiceManager})
    _module("scrapy.spiderloader", {"SpiderLoader": SpiderLoader})
    _module(
        "scrapy.statscollectors",
        {
            "MemoryStatsCollector": StatsCollector,
            "StatsCollector": StatsCollector,
        },
    )
    _module("scrapy.signalmanager", {"SignalManager": SignalManager})
    _module("scrapy.signals", _public(spideroxide_signals))
    _module(
        "scrapy.exceptions",
        {
            "CloseSpider": CloseSpider,
            "ContractFail": ContractFail,
            "DontCloseSpider": DontCloseSpider,
            "DownloadError": DownloadError,
            "DropItem": DropItem,
            "IgnoreRequest": IgnoreRequest,
            "NoActiveSpider": NoActiveSpider,
            "NotConfigured": NotConfigured,
            "NotSupported": NotSupported,
            "ScrapyDeprecationWarning": ScrapyDeprecationWarning,
            "ScrapyWarning": ScrapyWarning,
            "StopDownload": StopDownload,
            "UsageError": UsageError,
        },
    )

    _module("scrapy.downloadermiddlewares", {}, package=True)
    downloader_modules = {
        "offsite": {"OffsiteMiddleware": OffsiteMiddleware},
        "httpauth": {"HttpAuthMiddleware": HttpAuthMiddleware},
        "downloadtimeout": {"DownloadTimeoutMiddleware": DownloadTimeoutMiddleware},
        "defaultheaders": {"DefaultHeadersMiddleware": DefaultHeadersMiddleware},
        "useragent": {"UserAgentMiddleware": UserAgentMiddleware},
        "retry": {
            "RetryMiddleware": RetryMiddleware,
            "get_retry_request": get_retry_request,
        },
        "redirect": {
            "MetaRefreshMiddleware": MetaRefreshMiddleware,
            "RedirectMiddleware": RedirectMiddleware,
        },
        "httpcompression": {"HttpCompressionMiddleware": HttpCompressionMiddleware},
        "cookies": {"CookiesMiddleware": CookiesMiddleware},
        "httpproxy": {"HttpProxyMiddleware": HttpProxyMiddleware},
        "stats": {
            "DownloaderStats": DownloaderStatsMiddleware,
            "DownloaderStatsMiddleware": DownloaderStatsMiddleware,
        },
        "httpcache": {"HttpCacheMiddleware": HttpCacheMiddleware},
        "robotstxt": {"RobotsTxtMiddleware": RobotsTxtMiddleware},
    }
    for module_name, attributes in downloader_modules.items():
        _module(f"scrapy.downloadermiddlewares.{module_name}", attributes)

    _module("scrapy.spidermiddlewares", {}, package=True)
    spider_modules = {
        "base": {"BaseSpiderMiddleware": spideroxide.BaseSpiderMiddleware},
        "start": {"StartSpiderMiddleware": StartSpiderMiddleware},
        "httperror": {
            "HttpError": HttpError,
            "HttpErrorMiddleware": HttpErrorMiddleware,
        },
        "referer": {
            "DefaultReferrerPolicy": DefaultReferrerPolicy,
            "NoReferrerPolicy": NoReferrerPolicy,
            "NoReferrerWhenDowngradePolicy": NoReferrerWhenDowngradePolicy,
            "OriginPolicy": OriginPolicy,
            "OriginWhenCrossOriginPolicy": OriginWhenCrossOriginPolicy,
            "RefererMiddleware": RefererMiddleware,
            "ReferrerPolicy": ReferrerPolicy,
            "SameOriginPolicy": SameOriginPolicy,
            "StrictOriginPolicy": StrictOriginPolicy,
            "StrictOriginWhenCrossOriginPolicy": StrictOriginWhenCrossOriginPolicy,
            "UnsafeUrlPolicy": UnsafeUrlPolicy,
        },
        "urllength": {"UrlLengthMiddleware": UrlLengthMiddleware},
        "depth": {"DepthMiddleware": DepthMiddleware},
        "metacopy": {"MetaCopyDetectionMiddleware": MetaCopyDetectionMiddleware},
    }
    for module_name, attributes in spider_modules.items():
        _module(f"scrapy.spidermiddlewares.{module_name}", attributes)

    _module(
        "scrapy.pipelines",
        {"ItemPipelineManager": ItemPipelineManager},
        package=True,
    )
    _module("scrapy.pipelines.media", {"MediaPipeline": MediaPipeline})
    _module(
        "scrapy.pipelines.files",
        {
            "FSFilesStore": FSFilesStore,
            "FTPFilesStore": FTPFilesStore,
            "FileInfo": FileInfo,
            "FilesPipeline": FilesPipeline,
            "GCSFilesStore": GCSFilesStore,
            "S3FilesStore": S3FilesStore,
        },
    )
    _module(
        "scrapy.pipelines.images",
        {
            "ImageException": ImageException,
            "ImagesPipeline": ImagesPipeline,
        },
    )

    exporters = {
        "BaseItemExporter": BaseItemExporter,
        "CsvItemExporter": CsvItemExporter,
        "JsonItemExporter": JsonItemExporter,
        "JsonLinesItemExporter": JsonLinesItemExporter,
        "MarshalItemExporter": MarshalItemExporter,
        "PickleItemExporter": PickleItemExporter,
        "PprintItemExporter": PprintItemExporter,
        "PythonItemExporter": PythonItemExporter,
        "XmlItemExporter": XmlItemExporter,
    }
    _module("scrapy.exporters", exporters)
    _module("scrapy.extensions", {}, package=True)
    _module(
        "scrapy.extensions.feedexport",
        {
            "BlockingFeedStorage": BlockingFeedStorage,
            "FeedExporter": FeedExporter,
            "FeedSlot": FeedSlot,
            "FileFeedStorage": FileFeedStorage,
            "FTPFeedStorage": FTPFeedStorage,
            "GCSFeedStorage": GCSFeedStorage,
            "ItemFilter": ItemFilter,
            "S3FeedStorage": S3FeedStorage,
            "StdoutFeedStorage": StdoutFeedStorage,
            "apply_uri_params": apply_uri_params,
        },
    )
    _module(
        "scrapy.extensions.postprocessing",
        {
            "Bz2Plugin": Bz2Plugin,
            "GzipPlugin": GzipPlugin,
            "LZMAPlugin": LZMAPlugin,
            "PostProcessingManager": PostProcessingManager,
        },
    )
    _module("scrapy.extensions.corestats", {"CoreStats": CoreStats})
    _module("scrapy.extensions.logstats", {"LogStats": LogStats})
    _module("scrapy.extensions.logcount", {"LogCount": LogCount})
    _module("scrapy.extensions.closespider", {"CloseSpider": CloseSpiderExtension})
    _module("scrapy.extensions.memusage", {"MemoryUsage": MemoryUsage})
    _module("scrapy.extensions.memdebug", {"MemoryDebugger": MemoryDebugger})
    _module("scrapy.extensions.spiderstate", {"SpiderState": SpiderState})
    _module("scrapy.extension", {"ExtensionManager": ExtensionManager})

    _module("scrapy.core", {}, package=True)
    _module("scrapy.core.scheduler", {"Scheduler": Scheduler})
    _module("scrapy.core.downloader", {}, package=True)
    _module(
        "scrapy.core.downloader.middleware",
        {"DownloaderMiddlewareManager": DownloaderMiddlewareManager},
    )
    _module(
        "scrapy.core.downloader.handlers",
        {
            "BaseDownloadHandler": BaseDownloadHandler,
            "DownloadHandlers": DownloadHandlers,
        },
        package=True,
    )
    handler_modules = {
        "http": {"HTTPDownloadHandler": HTTPDownloadHandler},
        "http10": {"HTTP10DownloadHandler": HTTPDownloadHandler},
        "http11": {"HTTP11DownloadHandler": HTTPDownloadHandler},
        "file": {"FileDownloadHandler": FileDownloadHandler},
        "datauri": {"DataURIDownloadHandler": DataURIDownloadHandler},
        "ftp": {"FTPDownloadHandler": FTPDownloadHandler},
        "s3": {"S3DownloadHandler": S3DownloadHandler},
    }
    for module_name, attributes in handler_modules.items():
        _module(f"scrapy.core.downloader.handlers.{module_name}", attributes)
    _module(
        "scrapy.core.spidermw",
        {"SpiderMiddlewareManager": SpiderMiddlewareManager},
    )
    _module("scrapy.middleware", {}, package=True)
    _module(
        "scrapy.middleware",
        {
            "DownloaderMiddlewareManager": DownloaderMiddlewareManager,
            "SpiderMiddlewareManager": SpiderMiddlewareManager,
        },
        package=True,
    )

    _module(
        "scrapy.dupefilters",
        {
            "BaseDupeFilter": BaseDupeFilter,
            "RFPDupeFilter": RFPDupeFilter,
        },
    )
    queues = {
        "FifoMemoryQueue": FifoMemoryQueue,
        "LifoMemoryQueue": LifoMemoryQueue,
        "MarshalFifoDiskQueue": MarshalFifoDiskQueue,
        "MarshalLifoDiskQueue": MarshalLifoDiskQueue,
        "PickleFifoDiskQueue": PickleFifoDiskQueue,
        "PickleLifoDiskQueue": PickleLifoDiskQueue,
    }
    _module("scrapy.squeues", queues)
    _module("scrapy.pqueues", queues)
    _module(
        "scrapy.responsetypes", {"ResponseTypes": ResponseTypes, "responsetypes": responsetypes}
    )
    _module(
        "scrapy.extensions.httpcache",
        {
            "DbmCacheStorage": NativeHttpCacheStorage,
            "DummyPolicy": DummyPolicy,
            "FilesystemCacheStorage": NativeHttpCacheStorage,
            "RFC2616Policy": RFC2616Policy,
        },
    )

    _module("scrapy.utils", {}, package=True)
    _module(
        "scrapy.utils.misc",
        {
            "arg_to_iter": arg_to_iter,
            "build_from_crawler": build_from_crawler,
            "load_object": load_object,
        },
    )
    _module(
        "scrapy.utils.python",
        {
            "global_object_name": global_object_name,
            "to_bytes": to_bytes,
            "to_unicode": to_unicode,
            "without_none_values": without_none_values,
        },
    )
    _module(
        "scrapy.utils.project",
        {
            "closest_scrapy_cfg": closest_scrapy_cfg,
            "data_path": data_path,
            "find_projects": find_projects,
            "get_project_settings": get_project_settings,
            "inside_project": inside_project,
            "project_data_dir": project_data_dir,
        },
    )
    _module("scrapy.utils.iterators", {"csviter": csviter, "xmliter_lxml": xmliter_lxml})
    _module(
        "scrapy.utils.request",
        {
            "RequestFingerprinter": RequestFingerprinter,
            "fingerprint": fingerprint,
            "referer_str": referer_str,
            "request_from_dict": request_from_dict,
            "request_to_curl": request_to_curl,
        },
    )
    _module("scrapy.utils.response", {"response_status_message": response_status_message})
    _module("scrapy.utils.spider", {"iterate_spider_output": iterate_spider_output})
    _module(
        "scrapy.utils.sitemap",
        {
            "Sitemap": Sitemap,
            "sitemap_urls_from_robots": sitemap_urls_from_robots,
        },
    )
    _module("scrapy.utils.url", {"url_is_from_any_domain": url_is_from_any_domain})
    _module("scrapy.utils.trackref", {"live_refs": live_refs, "object_ref": object_ref})
