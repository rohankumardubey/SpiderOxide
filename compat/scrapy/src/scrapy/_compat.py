from __future__ import annotations

import inspect
import logging
import os
import sys
import warnings
import xmlrpc.client
from collections.abc import AsyncIterable, Iterable, Mapping
from configparser import ConfigParser
from importlib.machinery import ModuleSpec
from pathlib import Path
from types import ModuleType
from typing import Any, Protocol
from urllib.parse import urlparse, urlsplit, urlunparse

from w3lib.http import headers_dict_to_raw
from w3lib.url import canonicalize_url

import spideroxide
from spideroxide import signals as spideroxide_signals
from spideroxide.api import DupeFilter, Scheduler, fingerprint_request
from spideroxide.asyncutils import (
    AsyncioLoopingCall,
    CallLaterResult,
    as_async_generator,
    call_later,
    collect_asyncgen,
    create_looping_call,
    is_asyncio_available,
    run_in_thread,
    sleep,
)
from spideroxide.compathelpers import (
    MutableAsyncChain,
    MutableChain,
    binary_is_text,
    garbage_collect,
    get_func_args,
    get_func_args_dict,
    get_spec,
    is_generator_with_return_value,
    is_listlike,
    memoizemethod_noargs,
    re_rsearch,
    rel_has_nofollow,
    set_environ,
    unique,
    walk_callable,
    walk_modules,
    walk_modules_iter,
    warn_on_generator_with_return_value,
)
from spideroxide.compatutils import (
    ScrapyJSONEncoder,
    add_http_if_no_scheme,
    guess_scheme,
    job_dir,
    strip_url,
    url_has_any_extension,
    url_is_from_spider,
)
from spideroxide.components import build_from_crawler, load_object
from spideroxide.curl import curl_to_request_kwargs
from spideroxide.deprecateutils import (
    DEPRECATION_RULES,
    argument_is_required,
    attribute,
    create_deprecated_class,
    method_is_overridden,
    update_classpath,
    warn_on_deprecated_spider_attribute,
)
from spideroxide.downloader import _response_type
from spideroxide.exceptions import (
    ContractFail,
    NotConfigured,
    ScrapyDeprecationWarning,
    UsageError,
)
from spideroxide.logformatter import LogFormatter, LogFormatterResult, logformatter_adapter
from spideroxide.logutils import (
    DEFAULT_LOGGING,
    LogCounterHandler,
    SpiderLoggerAdapter,
    StreamLogger,
    TopLevelFormatter,
    configure_logging,
    get_scrapy_root_handler,
    install_scrapy_root_handler,
    log_scrapy_info,
)
from spideroxide.middleware import (
    DownloaderMiddlewareManager,
    ItemPipelineManager,
    SpiderMiddlewareManager,
)
from spideroxide.remote_control import RemoteControl
from spideroxide.responseutils import (
    get_base_url,
    get_meta_refresh,
    open_in_browser,
    response_from_dict,
)
from spideroxide.settings import (
    DEFAULT_SETTINGS,
    PRIORITIES,
    BaseSettings,
    Settings,
    SettingsAttribute,
    get_settings_priority,
)
from spideroxide.spiderloader import iter_spider_classes
from spideroxide.telnet import TelnetConsole, update_telnet_vars
from spideroxide.twisted_compat import (
    deferred_f_from_coro_f,
    deferred_from_coro,
    deferred_to_future,
    ensure_awaitable,
    install_reactor,
    is_asyncio_reactor_installed,
    is_reactor_installed,
    maybe_deferred_to_future,
    verify_installed_asyncio_event_loop,
    verify_installed_reactor,
)

logger = logging.getLogger("scrapy.utils.spider")


class ScrapyWarning(Warning):
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


class RequestFingerprinterProtocol(Protocol):
    def fingerprint(self, request: object) -> bytes: ...


def request_fingerprint(request: object) -> str:
    return fingerprint_request(request).hex()


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


def global_object_name(obj: object) -> str:
    return f"{obj.__module__}.{obj.__qualname__}"  # type: ignore[attr-defined]


def to_bytes(
    text: str | bytes,
    encoding: str | None = None,
    errors: str = "strict",
) -> bytes:
    if isinstance(text, bytes):
        return text
    if not isinstance(text, str):
        raise TypeError(f"to_bytes must receive a str or bytes object, got {type(text).__name__}")
    return text.encode(encoding or "utf-8", errors)


def to_unicode(
    text: str | bytes,
    encoding: str | None = None,
    errors: str = "strict",
) -> str:
    if isinstance(text, str):
        return text
    if not isinstance(text, bytes):
        raise TypeError(f"to_unicode must receive a bytes or str object, got {type(text).__name__}")
    return text.decode(encoding or "utf-8", errors)


def without_none_values(iterable: Mapping[Any, Any] | Iterable[Any]) -> object:
    if isinstance(iterable, Mapping):
        return {key: value for key, value in iterable.items() if value is not None}
    return type(iterable)(value for value in iterable if value is not None)


def arg_to_iter(arg: object) -> Iterable[object]:
    if arg is None:
        return ()
    if not isinstance(arg, (str, bytes, Mapping)) and hasattr(arg, "__iter__"):
        return arg
    return [arg]


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
        except ImportError as error:
            warnings.warn(
                f"Cannot import scrapy settings module {module_name}: {error}",
                stacklevel=2,
            )
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
    if not inside_project():
        raise NotConfigured("Not inside a project")
    config_path = closest_scrapy_cfg()
    if not config_path:
        raise NotConfigured("Unable to find scrapy.cfg file to infer project data dir")
    config = ConfigParser()
    config.read(config_path)
    if config.has_option("datadir", project):
        directory = Path(config.get("datadir", project))
    else:
        directory = (Path(config_path).parent / ".scrapy").resolve()
    directory.mkdir(parents=True, exist_ok=True)
    return str(directory)


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
    if not isinstance(request_type, type) or not issubclass(request_type, spideroxide.Request):
        raise TypeError(f"request class must be a Request subclass, got {request_type!r}")
    data = {name: value for name, value in data.items() if name in request_type.attributes}
    for field in ("callback", "errback"):
        value = data.get(field)
        if value and spider is not None:
            name = str(value)
            try:
                data[field] = getattr(spider, name)
            except AttributeError:
                raise ValueError(f"Method {name!r} not found in: {spider}") from None
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


def request_httprepr(request: spideroxide.Request) -> bytes:
    parsed = urlparse(request.url)
    path = urlunparse(("", "", parsed.path or "/", parsed.params, parsed.query, ""))
    result = to_bytes(request.method) + b" " + to_bytes(path) + b" HTTP/1.1\r\n"
    result += b"Host: " + to_bytes(parsed.hostname or "") + b"\r\n"
    if request.headers:
        result += headers_dict_to_raw(request.headers.to_scrapy_dict()) + b"\r\n"
    return result + b"\r\n" + request.body


def referer_str(request: object) -> str | None:
    value = request.headers.get("Referer")
    return None if value is None else value.decode("utf-8", errors="replace")


def url_is_from_any_domain(url: str, domains: Iterable[str]) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    return any(host == domain.lower() or host.endswith(f".{domain.lower()}") for domain in domains)


# Scrapy 2.19 uses Twisted's status phrases, which differ from stdlib HTTPStatus.
_STATUS_PHRASES = {
    100: "Continue",
    101: "Switching Protocols",
    200: "OK",
    201: "Created",
    202: "Accepted",
    203: "Non-Authoritative Information",
    204: "No Content",
    205: "Reset Content.",
    206: "Partial Content",
    207: "Multi-Status",
    300: "Multiple Choices",
    301: "Moved Permanently",
    302: "Found",
    303: "See Other",
    304: "Not Modified",
    305: "Use Proxy",
    307: "Temporary Redirect",
    308: "Permanent Redirect",
    400: "Bad Request",
    401: "Unauthorized",
    402: "Payment Required",
    403: "Forbidden",
    404: "Not Found",
    405: "Method Not Allowed",
    406: "Not Acceptable",
    407: "Proxy Authentication Required",
    408: "Request Time-out",
    409: "Conflict",
    410: "Gone",
    411: "Length Required",
    412: "Precondition Failed",
    413: "Request Entity Too Large",
    414: "Request-URI Too Long",
    415: "Unsupported Media Type",
    416: "Requested Range not satisfiable",
    417: "Expectation Failed",
    418: "I'm a teapot",
    500: "Internal Server Error",
    501: "Not Implemented",
    502: "Bad Gateway",
    503: "Service Unavailable",
    504: "Gateway Time-out",
    505: "HTTP Version not supported",
    507: "Insufficient Storage Space",
    510: "Not Extended",
}


def response_status_message(status: bytes | float | str | int) -> str:
    code = int(status)
    return f"{code} {_STATUS_PHRASES.get(code, 'Unknown Status')}"


def iterate_spider_output(result: object) -> object:
    if inspect.isawaitable(result) or isinstance(result, AsyncIterable):
        return result
    return arg_to_iter(result)


def spidercls_for_request(
    spider_loader: spideroxide.SpiderLoader,
    request: spideroxide.Request,
    default_spidercls: type[spideroxide.Spider] | None = None,
    log_none: bool = False,
    log_multiple: bool = False,
) -> type[spideroxide.Spider] | None:
    names = spider_loader.find_by_request(request)
    if len(names) == 1:
        return spider_loader.load(names[0])
    if len(names) > 1 and log_multiple:
        logger.error(
            "More than one spider can handle: %(request)s - %(snames)s",
            {"request": request, "snames": ", ".join(names)},
        )
    if not names and log_none:
        logger.error("Unable to find spider that handles: %(request)s", {"request": request})
    return default_spidercls


class DefaultSpider(spideroxide.Spider):
    name = "default"


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
        CannotResolveHostError,
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
        DownloadCancelledError,
        DownloadConnectionRefusedError,
        DownloadError,
        DownloaderStatsMiddleware,
        DownloadFailedError,
        DownloadHandlers,
        DownloadTimeoutError,
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
        ResponseDataLossError,
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
        UnsupportedURLSchemeError,
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
    from spideroxide.operational import AutoThrottle, PeriodicLog
    from spideroxide.operational import CloseSpider as CloseSpiderExtension
    from spideroxide.pipelines import FSFilesStore
    from spideroxide.robots import RobotsTxtMiddleware
    from spideroxide.statsmailer import StatsMailer
    from spideroxide.trackref import (
        format_live_refs,
        get_oldest,
        iter_all,
        live_refs,
        object_ref,
        print_live_refs,
    )
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
            "REQUEST_FINGERPRINTER_CLASS": "scrapy.utils.request.RequestFingerprinter",
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
        "scrapy.logformatter",
        {"LogFormatter": LogFormatter, "LogFormatterResult": LogFormatterResult},
    )
    _module(
        "scrapy.exceptions",
        {
            "CannotResolveHostError": CannotResolveHostError,
            "CloseSpider": CloseSpider,
            "ContractFail": ContractFail,
            "DontCloseSpider": DontCloseSpider,
            "DownloadCancelledError": DownloadCancelledError,
            "DownloadConnectionRefusedError": DownloadConnectionRefusedError,
            "DownloadError": DownloadError,
            "DownloadFailedError": DownloadFailedError,
            "DownloadTimeoutError": DownloadTimeoutError,
            "DropItem": DropItem,
            "IgnoreRequest": IgnoreRequest,
            "NoActiveSpider": NoActiveSpider,
            "NotConfigured": NotConfigured,
            "NotSupported": NotSupported,
            "ScrapyDeprecationWarning": ScrapyDeprecationWarning,
            "ScrapyWarning": ScrapyWarning,
            "ResponseDataLossError": ResponseDataLossError,
            "StopDownload": StopDownload,
            "UnsupportedURLSchemeError": UnsupportedURLSchemeError,
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
    _module("scrapy.extensions.throttle", {"AutoThrottle": AutoThrottle})
    _module("scrapy.extensions.periodic_log", {"PeriodicLog": PeriodicLog})
    _module("scrapy.extensions.statsmailer", {"StatsMailer": StatsMailer})
    _module(
        "scrapy.extensions.telnet",
        {"TelnetConsole": TelnetConsole, "update_telnet_vars": update_telnet_vars},
    )
    _module("scrapy.extensions.remote_control", {"RemoteControl": RemoteControl})
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
        "scrapy.utils.asyncgen",
        {
            "as_async_generator": as_async_generator,
            "collect_asyncgen": collect_asyncgen,
        },
    )
    _module(
        "scrapy.utils.asyncio",
        {
            "AsyncioLoopingCall": AsyncioLoopingCall,
            "CallLaterResult": CallLaterResult,
            "call_later": call_later,
            "create_looping_call": create_looping_call,
            "is_asyncio_available": is_asyncio_available,
            "run_in_thread": run_in_thread,
            "sleep": sleep,
        },
    )
    _module(
        "scrapy.utils.defer",
        {
            "deferred_f_from_coro_f": deferred_f_from_coro_f,
            "deferred_from_coro": deferred_from_coro,
            "deferred_to_future": deferred_to_future,
            "ensure_awaitable": ensure_awaitable,
            "maybe_deferred_to_future": maybe_deferred_to_future,
        },
    )
    _module(
        "scrapy.utils.reactor",
        {
            "install_reactor": install_reactor,
            "is_asyncio_reactor_installed": is_asyncio_reactor_installed,
            "is_reactor_installed": is_reactor_installed,
            "verify_installed_asyncio_event_loop": verify_installed_asyncio_event_loop,
            "verify_installed_reactor": verify_installed_reactor,
        },
    )
    _module(
        "scrapy.utils.misc",
        {
            "arg_to_iter": arg_to_iter,
            "build_from_crawler": build_from_crawler,
            "is_generator_with_return_value": is_generator_with_return_value,
            "load_object": load_object,
            "rel_has_nofollow": rel_has_nofollow,
            "set_environ": set_environ,
            "walk_callable": walk_callable,
            "walk_modules": walk_modules,
            "walk_modules_iter": walk_modules_iter,
            "warn_on_generator_with_return_value": warn_on_generator_with_return_value,
        },
    )
    _module(
        "scrapy.utils.deprecate",
        {
            "DEPRECATION_RULES": DEPRECATION_RULES,
            "argument_is_required": argument_is_required,
            "attribute": attribute,
            "create_deprecated_class": create_deprecated_class,
            "method_is_overridden": method_is_overridden,
            "update_classpath": update_classpath,
            "warn_on_deprecated_spider_attribute": warn_on_deprecated_spider_attribute,
        },
    )
    _module(
        "scrapy.utils.python",
        {
            "MutableAsyncChain": MutableAsyncChain,
            "MutableChain": MutableChain,
            "binary_is_text": binary_is_text,
            "garbage_collect": garbage_collect,
            "get_func_args": get_func_args,
            "get_func_args_dict": get_func_args_dict,
            "get_spec": get_spec,
            "global_object_name": global_object_name,
            "is_listlike": is_listlike,
            "memoizemethod_noargs": memoizemethod_noargs,
            "re_rsearch": re_rsearch,
            "to_bytes": to_bytes,
            "to_unicode": to_unicode,
            "unique": unique,
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
    _module("scrapy.utils.job", {"job_dir": job_dir})
    _module("scrapy.utils.curl", {"curl_to_request_kwargs": curl_to_request_kwargs})
    _module("scrapy.utils.iterators", {"csviter": csviter, "xmliter_lxml": xmliter_lxml})
    _module(
        "scrapy.utils.log",
        {
            "DEFAULT_LOGGING": DEFAULT_LOGGING,
            "LogCounterHandler": LogCounterHandler,
            "SpiderLoggerAdapter": SpiderLoggerAdapter,
            "StreamLogger": StreamLogger,
            "TopLevelFormatter": TopLevelFormatter,
            "configure_logging": configure_logging,
            "get_scrapy_root_handler": get_scrapy_root_handler,
            "install_scrapy_root_handler": install_scrapy_root_handler,
            "log_scrapy_info": log_scrapy_info,
            "logformatter_adapter": logformatter_adapter,
        },
    )
    _module("scrapy.utils.serialize", {"ScrapyJSONEncoder": ScrapyJSONEncoder})
    _module(
        "scrapy.utils.request",
        {
            "RequestFingerprinter": RequestFingerprinter,
            "RequestFingerprinterProtocol": RequestFingerprinterProtocol,
            "fingerprint": fingerprint,
            "referer_str": referer_str,
            "request_fingerprint": request_fingerprint,
            "request_from_dict": request_from_dict,
            "request_httprepr": request_httprepr,
            "request_to_curl": request_to_curl,
        },
    )
    _module(
        "scrapy.utils.response",
        {
            "get_base_url": get_base_url,
            "get_meta_refresh": get_meta_refresh,
            "open_in_browser": open_in_browser,
            "response_from_dict": response_from_dict,
            "response_status_message": response_status_message,
        },
    )
    _module(
        "scrapy.utils.spider",
        {
            "DefaultSpider": DefaultSpider,
            "iter_spider_classes": iter_spider_classes,
            "iterate_spider_output": iterate_spider_output,
            "spidercls_for_request": spidercls_for_request,
        },
    )
    _module(
        "scrapy.utils.sitemap",
        {
            "Sitemap": Sitemap,
            "sitemap_urls_from_robots": sitemap_urls_from_robots,
        },
    )
    _module(
        "scrapy.utils.url",
        {
            "add_http_if_no_scheme": add_http_if_no_scheme,
            "canonicalize_url": canonicalize_url,
            "guess_scheme": guess_scheme,
            "strip_url": strip_url,
            "url_has_any_extension": url_has_any_extension,
            "url_is_from_any_domain": url_is_from_any_domain,
            "url_is_from_spider": url_is_from_spider,
        },
    )
    _module(
        "scrapy.utils.trackref",
        {
            "format_live_refs": format_live_refs,
            "get_oldest": get_oldest,
            "iter_all": iter_all,
            "live_refs": live_refs,
            "object_ref": object_ref,
            "print_live_refs": print_live_refs,
        },
    )
