from __future__ import annotations

import copy
import importlib
import json
from collections.abc import Iterable, Iterator, Mapping, MutableMapping
from types import ModuleType
from typing import Any

PRIORITIES = {
    "default": 0,
    "command": 10,
    "addon": 15,
    "project": 20,
    "spider": 30,
    "cmdline": 40,
}

DEFAULT_SETTINGS: dict[str, object] = {
    "CONCURRENT_REQUESTS": 16,
    "CONCURRENT_REQUESTS_PER_DOMAIN": 8,
    "ENGINE_BACKEND": "python",
    "ENGINE_MAX_PENDING": 0,
    "BOT_NAME": "scrapybot",
    "NEWSPIDER_MODULE": "",
    "COMMANDS_MODULE": "",
    "EDITOR": "vi",
    "TEMPLATES_DIR": None,
    "SPIDER_MODULES": [],
    "SPIDER_LOADER_WARN_ONLY": False,
    "LOG_FORMATTER": "spideroxide.logformatter.LogFormatter",
    "DEFAULT_DROPITEM_LOG_LEVEL": "WARNING",
    "MAIL_HOST": "localhost",
    "MAIL_FROM": "scrapy@localhost",
    "MAIL_USER": None,
    "MAIL_PASS": None,
    "MAIL_PORT": 25,
    "MAIL_TLS": False,
    "MAIL_SSL": False,
    "SPIDER_CONTRACTS_BASE": {
        "scrapy.contracts.default.UrlContract": 1,
        "scrapy.contracts.default.CallbackKeywordArgumentsContract": 1,
        "scrapy.contracts.default.MetadataContract": 1,
        "scrapy.contracts.default.MethodContract": 1,
        "scrapy.contracts.default.BodyContract": 1,
        "scrapy.contracts.default.HeaderContract": 1,
        "scrapy.contracts.default.CookieContract": 1,
        "scrapy.contracts.default.ReturnsContract": 2,
        "scrapy.contracts.default.ScrapesContract": 3,
    },
    "SPIDER_CONTRACTS": {},
    "ADDONS": {},
    "SERVICES_BASE": {},
    "SERVICES": {},
    "JOBDIR": None,
    "SCHEDULER_DEBUG": False,
    "SCHEDULER_MEMORY_QUEUE": "scrapy.squeues.LifoMemoryQueue",
    "SCHEDULER_DISK_QUEUE": "scrapy.squeues.PickleLifoDiskQueue",
    "SCHEDULER_START_MEMORY_QUEUE": "scrapy.squeues.FifoMemoryQueue",
    "SCHEDULER_START_DISK_QUEUE": "scrapy.squeues.PickleFifoDiskQueue",
    "DOWNLOAD_DELAY": 0.0,
    "RANDOMIZE_DOWNLOAD_DELAY": True,
    "DOWNLOAD_SLOTS": {},
    "DOWNLOAD_TIMEOUT": 180.0,
    "DOWNLOAD_MAXSIZE": 1024 * 1024 * 1024,
    "DOWNLOAD_WARNSIZE": 32 * 1024 * 1024,
    "DOWNLOAD_FAIL_ON_DATALOSS": True,
    "DOWNLOAD_BIND_ADDRESS": None,
    "DOWNLOAD_VERIFY_CERTIFICATES": False,
    "DOWNLOAD_TLS_MIN_VERSION": None,
    "DOWNLOAD_TLS_MAX_VERSION": None,
    "DOWNLOADER_CLIENTCONTEXTFACTORY": "SENTINEL",
    "DOWNLOADER_CLIENT_TLS_CIPHERS": "DEFAULT",
    "DOWNLOADER_CLIENT_TLS_METHOD": "TLS",
    "DOWNLOADER_CLIENT_TLS_VERBOSE_LOGGING": False,
    "DOWNLOADER_CLIENT_CERTIFICATE": None,
    "DOWNLOADER_CLIENT_KEY": None,
    "DOWNLOADER_CLIENT_KEY_PASSWORD": None,
    "HTTPX_HTTP2_ENABLED": False,
    "HTTP2_MAX_FRAME_SIZE": 16384,
    "DNSCACHE_ENABLED": True,
    "DNSCACHE_SIZE": 10000,
    "DNS_TIMEOUT": 60,
    "DNS_RESOLVER": "scrapy.resolver.CachingThreadedResolver",
    "TWISTED_DNS_RESOLVER": "scrapy.resolver.CachingThreadedResolver",
    "DOWNLOADER_BACKEND": "python",
    "DOWNLOAD_HANDLERS_BASE": {
        "data": "spideroxide.downloadhandlers.DataURIDownloadHandler",
        "file": "spideroxide.downloadhandlers.FileDownloadHandler",
        "ftp": "spideroxide.downloadhandlers.FTPDownloadHandler",
        "http": "spideroxide.downloadhandlers.HTTPDownloadHandler",
        "https": "spideroxide.downloadhandlers.HTTPDownloadHandler",
        "s3": "spideroxide.downloadhandlers.S3DownloadHandler",
    },
    "DOWNLOAD_HANDLERS": {},
    "FTP_USER": "anonymous",
    "FTP_PASSWORD": "guest",
    "FTP_PASSIVE_MODE": True,
    "MEDIA_ALLOW_REDIRECTS": False,
    "FILES_STORE": None,
    "FILES_EXPIRES": 90,
    "FILES_URLS_FIELD": "file_urls",
    "FILES_RESULT_FIELD": "files",
    "FILES_STORE_GCS_ACL": "",
    "FILES_STORE_S3_ACL": "private",
    "IMAGES_STORE": None,
    "IMAGES_EXPIRES": 90,
    "IMAGES_URLS_FIELD": "image_urls",
    "IMAGES_RESULT_FIELD": "images",
    "IMAGES_MIN_WIDTH": 0,
    "IMAGES_MIN_HEIGHT": 0,
    "IMAGES_THUMBS": {},
    "IMAGES_STORE_GCS_ACL": "",
    "IMAGES_STORE_S3_ACL": "private",
    "USER_AGENT": "SpiderOxide/0.1",
    "DEFAULT_REQUEST_HEADERS": {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en",
    },
    "HTTPAUTH_USER": "",
    "HTTPAUTH_PASS": "",
    "HTTPAUTH_DOMAIN": None,
    "DOWNLOADER_MIDDLEWARES_BASE": {
        "spideroxide.downloadermiddlewares.OffsiteMiddleware": 50,
        "spideroxide.robots.RobotsTxtMiddleware": 100,
        "spideroxide.downloadermiddlewares.HttpAuthMiddleware": 300,
        "spideroxide.downloadermiddlewares.DownloadTimeoutMiddleware": 350,
        "spideroxide.downloadermiddlewares.DefaultHeadersMiddleware": 400,
        "spideroxide.downloadermiddlewares.UserAgentMiddleware": 500,
        "spideroxide.retry.RetryMiddleware": 550,
        "spideroxide.redirect.MetaRefreshMiddleware": 580,
        "spideroxide.downloadermiddlewares.HttpCompressionMiddleware": 590,
        "spideroxide.redirect.RedirectMiddleware": 600,
        "spideroxide.cookies.CookiesMiddleware": 700,
        "spideroxide.proxy.HttpProxyMiddleware": 750,
        "spideroxide.downloadermiddlewares.DownloaderStatsMiddleware": 850,
        "spideroxide.httpcache.HttpCacheMiddleware": 900,
    },
    "DOWNLOADER_MIDDLEWARES": {},
    "DOWNLOADER_STATS": True,
    "HTTPPROXY_ENABLED": True,
    "HTTPPROXY_AUTH_ENCODING": "latin-1",
    "COOKIES_ENABLED": True,
    "COOKIES_DEBUG": False,
    "SPIDER_MIDDLEWARES_BASE": {
        "spideroxide.spidermiddlewares.StartSpiderMiddleware": 25,
        "spideroxide.spidermiddlewares.HttpErrorMiddleware": 50,
        "spideroxide.spidermiddlewares.RefererMiddleware": 700,
        "spideroxide.spidermiddlewares.UrlLengthMiddleware": 800,
        "spideroxide.depth.DepthMiddleware": 900,
        "spideroxide.spidermiddlewares.MetaCopyDetectionMiddleware": 1000,
    },
    "SPIDER_MIDDLEWARES": [],
    "ITEM_PIPELINES": [],
    "EXTENSIONS_BASE": {
        "spideroxide.operational.CoreStats": 0,
        "spideroxide.operational.MemoryUsage": 0,
        "spideroxide.operational.MemoryDebugger": 0,
        "spideroxide.operational.CloseSpider": 0,
        "spideroxide.operational.LogCount": 0,
        "spideroxide.operational.LogStats": 0,
        "spideroxide.operational.SpiderState": 0,
        "spideroxide.feedexport.FeedExporter": 0,
    },
    "EXTENSIONS": {},
    "LOG_LEVEL": "DEBUG",
    "LOG_ENABLED": True,
    "LOG_FILE": None,
    "LOG_FILE_APPEND": True,
    "LOG_ENCODING": "utf-8",
    "LOG_FORMAT": "%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    "LOG_DATEFORMAT": "%Y-%m-%d %H:%M:%S",
    "LOG_COLOR": True,
    "LOG_INSTALL_ROOT_HANDLER": True,
    "LOG_SHORT_NAMES": False,
    "LOG_STDOUT": False,
    "LOGSTATS_INTERVAL": 60.0,
    "CLOSESPIDER_TIMEOUT": 0.0,
    "CLOSESPIDER_ITEMCOUNT": 0,
    "CLOSESPIDER_PAGECOUNT": 0,
    "CLOSESPIDER_ERRORCOUNT": 0,
    "CLOSESPIDER_TIMEOUT_NO_ITEM": 0.0,
    "CLOSESPIDER_PAGECOUNT_NO_ITEM": 0,
    "MEMUSAGE_ENABLED": True,
    "MEMUSAGE_LIMIT_MB": 0,
    "MEMUSAGE_WARNING_MB": 0,
    "MEMUSAGE_CHECK_INTERVAL_SECONDS": 60.0,
    "MEMUSAGE_NOTIFY_MAIL": [],
    "MEMDEBUG_ENABLED": False,
    "PERIODIC_LOG_STATS": False,
    "PERIODIC_LOG_DELTA": False,
    "PERIODIC_LOG_TIMING_ENABLED": False,
    "FEEDS": {},
    "FEED_URI": None,
    "FEED_FORMAT": "jsonlines",
    "FEED_STORAGES_BASE": {
        "": "spideroxide.feedexport.FileFeedStorage",
        "file": "spideroxide.feedexport.FileFeedStorage",
        "ftp": "spideroxide.feedexport.FTPFeedStorage",
        "ftps": "spideroxide.feedexport.FTPFeedStorage",
        "gs": "spideroxide.feedexport.GCSFeedStorage",
        "s3": "spideroxide.feedexport.S3FeedStorage",
        "stdout": "spideroxide.feedexport.StdoutFeedStorage",
    },
    "FEED_STORAGES": {},
    "FEED_STORAGE_FTP_ACTIVE": False,
    "FEED_STORAGE_GCS_ACL": "",
    "FEED_STORAGE_S3_ACL": "",
    "FEED_STORAGE_CONCURRENCY": 4,
    "FEED_TEMPDIR": None,
    "AWS_ACCESS_KEY_ID": None,
    "AWS_SECRET_ACCESS_KEY": None,
    "AWS_SESSION_TOKEN": None,
    "AWS_ENDPOINT_URL": None,
    "AWS_REGION_NAME": None,
    "AWS_MAX_POOL_CONNECTIONS": None,
    "AWS_USE_SSL": None,
    "AWS_VERIFY": None,
    "REACTOR_THREADPOOL_MAXSIZE": 10,
    "GCS_PROJECT_ID": None,
    "FEED_EXPORTERS_BASE": {
        "json": "spideroxide.feedexport.JsonItemExporter",
        "jsonlines": "spideroxide.feedexport.JsonLinesItemExporter",
        "jsonl": "spideroxide.feedexport.JsonLinesItemExporter",
        "jl": "spideroxide.feedexport.JsonLinesItemExporter",
        "csv": "spideroxide.feedexport.CsvItemExporter",
        "xml": "spideroxide.feedexport.XmlItemExporter",
        "marshal": "spideroxide.feedexport.MarshalItemExporter",
        "pickle": "spideroxide.feedexport.PickleItemExporter",
    },
    "FEED_EXPORTERS": {},
    "FEED_EXPORT_ENCODING": None,
    "FEED_EXPORT_FIELDS": None,
    "FEED_EXPORT_INDENT": 0,
    "FEED_STORE_EMPTY": True,
    "FEED_EXPORT_BATCH_ITEM_COUNT": 0,
    "FEED_URI_PARAMS": None,
    "DEPTH_LIMIT": 0,
    "DEPTH_PRIORITY": 0,
    "DEPTH_STATS_VERBOSE": False,
    "REFERER_ENABLED": True,
    "REFERRER_POLICY": "scrapy-default",
    "REFERRER_POLICIES": {},
    "URLLENGTH_LIMIT": 2083,
    "HTTPERROR_ALLOW_ALL": False,
    "HTTPERROR_ALLOWED_CODES": [],
    "META_COPY_WARN_SKIP_KEYS": [],
    "RETRY_ENABLED": True,
    "RETRY_TIMES": 2,
    "RETRY_HTTP_CODES": [500, 502, 503, 504, 522, 524, 408, 429],
    "RETRY_PRIORITY_ADJUST": -1,
    "RETRY_EXCEPTIONS": ["spideroxide.exceptions.DownloadError"],
    "RETRY_GIVE_UP_LOG_LEVEL": "ERROR",
    "ROBOTSTXT_OBEY": False,
    "ROBOTSTXT_USER_AGENT": None,
    "REDIRECT_ENABLED": True,
    "REDIRECT_MAX_TIMES": 20,
    "REDIRECT_PRIORITY_ADJUST": 2,
    "METAREFRESH_ENABLED": True,
    "METAREFRESH_MAXDELAY": 100,
    "METAREFRESH_IGNORE_TAGS": ["noscript"],
    "COMPRESSION_ENABLED": True,
    "AUTOTHROTTLE_ENABLED": False,
    "AUTOTHROTTLE_START_DELAY": 5.0,
    "AUTOTHROTTLE_MAX_DELAY": 60.0,
    "AUTOTHROTTLE_TARGET_CONCURRENCY": 1.0,
    "AUTOTHROTTLE_DEBUG": False,
    "HTTPCACHE_ENABLED": False,
    "HTTPCACHE_POLICY": "spideroxide.httpcache.DummyPolicy",
    "HTTPCACHE_STORAGE": "spideroxide.httpcache.NativeHttpCacheStorage",
    "HTTPCACHE_DIR": "httpcache",
    "HTTPCACHE_EXPIRATION_SECS": 0,
    "HTTPCACHE_IGNORE_HTTP_CODES": [],
    "HTTPCACHE_IGNORE_MISSING": False,
    "HTTPCACHE_IGNORE_SCHEMES": ["file"],
    "HTTPCACHE_IGNORE_RESPONSE_CACHE_CONTROLS": [],
    "HTTPCACHE_ALWAYS_STORE": False,
    "HTTPCACHE_GZIP": False,
    "HTTPCACHE_DBM_MODULE": "dbm",
}


def get_settings_priority(priority: int | str) -> int:
    if isinstance(priority, str):
        return PRIORITIES[priority]
    return priority


class SettingsAttribute:
    def __init__(self, value: object, priority: int) -> None:
        self.value = value
        self.priority = (
            max(value.maxpriority(), priority) if isinstance(value, BaseSettings) else priority
        )

    def set(self, value: object, priority: int) -> None:
        if priority < self.priority:
            return
        if isinstance(self.value, BaseSettings) and isinstance(value, (BaseSettings, Mapping)):
            value = BaseSettings(value, priority=priority)
        self.value = value
        self.priority = priority

    def __repr__(self) -> str:
        return f"<SettingsAttribute value={self.value!r} priority={self.priority}>"


class BaseSettings(MutableMapping[str, object]):
    def __init__(
        self,
        values: (
            BaseSettings | Mapping[str, object] | Iterable[tuple[str, object]] | str | None
        ) = None,
        priority: int | str = "project",
    ) -> None:
        self.frozen = False
        self.attributes: dict[str, SettingsAttribute] = {}
        if values:
            self.update(values, priority)

    def _assert_mutability(self) -> None:
        if self.frozen:
            raise TypeError("Trying to modify an immutable Settings object")

    def set(self, name: str, value: object, priority: int | str = "project") -> None:
        self._assert_mutability()
        numeric_priority = get_settings_priority(priority)
        if name not in self.attributes:
            self.attributes[name] = (
                value
                if isinstance(value, SettingsAttribute)
                else SettingsAttribute(value, numeric_priority)
            )
        else:
            self.attributes[name].set(value, numeric_priority)

    def update_values(
        self,
        values: Mapping[str, object],
        priority: int | str = "project",
    ) -> None:
        self.update(values, priority)

    def setdict(
        self,
        values: (BaseSettings | Mapping[str, object] | Iterable[tuple[str, object]] | str | None),
        priority: int | str = "project",
    ) -> None:
        self.update(values, priority)

    def update(
        self,
        values: (
            BaseSettings | Mapping[str, object] | Iterable[tuple[str, object]] | str | None
        ) = None,
        priority: int | str = "project",
        **kwargs: object,
    ) -> None:
        self._assert_mutability()
        if isinstance(values, str):
            values = json.loads(values)
        if values is not None:
            if isinstance(values, BaseSettings):
                for name, value in values.items():
                    self.set(name, value, values.getpriority(name) or 0)
            else:
                items = values.items() if isinstance(values, Mapping) else values
                for name, value in items:
                    self.set(name, value, priority)
        for name, value in kwargs.items():
            self.set(name, value, priority)

    def setmodule(
        self,
        module: ModuleType | str,
        priority: int | str = "project",
    ) -> None:
        self._assert_mutability()
        if isinstance(module, str):
            module = importlib.import_module(module)
        for name in dir(module):
            if name.isupper():
                self.set(name, getattr(module, name), priority)

    def setdefault(
        self,
        name: str,
        default: object = None,
        priority: int | str = "project",
    ) -> object:
        if name not in self:
            self.set(name, default, priority)
            return default
        return self.attributes[name].value

    def delete(self, name: str, priority: int | str = "project") -> None:
        if name not in self:
            raise KeyError(name)
        self._assert_mutability()
        numeric_priority = get_settings_priority(priority)
        current_priority = self.getpriority(name)
        if current_priority is not None and numeric_priority >= current_priority:
            del self.attributes[name]

    def freeze(self) -> None:
        self.frozen = True

    def get(self, name: str, default: Any = None) -> Any:
        value = self[name]
        return default if value is None else value

    def getbool(self, name: str, default: bool = False) -> bool:
        value = self.get(name, default)
        try:
            return bool(int(value))  # type: ignore[arg-type]
        except ValueError:
            if value in {"True", "true"}:
                return True
            if value in {"False", "false"}:
                return False
            raise ValueError(
                "Supported values for boolean settings are 0/1, True/False, "
                "'0'/'1', 'True'/'False' and 'true'/'false'"
            ) from None

    def getint(self, name: str, default: int = 0) -> int:
        value = self.get(name, default)
        return default if value is None else int(value)

    def getfloat(self, name: str, default: float = 0.0) -> float:
        value = self.get(name, default)
        return default if value is None else float(value)

    def getpriority(self, name: str) -> int | None:
        setting = self.attributes.get(name)
        return None if setting is None else setting.priority

    def maxpriority(self) -> int:
        if self.attributes:
            return max(attribute.priority for attribute in self.attributes.values())
        return get_settings_priority("default")

    def getlist(self, name: str, default: list[object] | None = None) -> list[object]:
        value = self.get(name, default or [])
        if not value:
            return []
        if isinstance(value, str):
            value = value.split(",")
        return list(value)  # type: ignore[arg-type]

    def getdict(self, name: str, default: Mapping[str, Any] | None = None) -> dict[str, Any]:
        value = self.get(name, default or {})
        if isinstance(value, str):
            value = json.loads(value)
        return dict(value)

    def getdictorlist(
        self,
        name: str,
        default: dict[Any, Any] | list[Any] | tuple[Any, ...] | None = None,
    ) -> dict[Any, Any] | list[Any]:
        value = self.get(name, default)
        if value is None:
            return {}
        if isinstance(value, str):
            try:
                decoded = json.loads(value)
                if not isinstance(decoded, (dict, list)):
                    raise ValueError
                return decoded
            except ValueError:
                return value.split(",")
        if isinstance(value, tuple):
            return list(value)
        if not isinstance(value, (dict, list)):
            raise ValueError(
                f"Setting {name!r} must be a dict, list, tuple, or string, "
                f"got {type(value).__name__}: {value!r}"
            )
        return copy.deepcopy(value)

    def getwithbase(self, name: str) -> BaseSettings:
        if not isinstance(name, str):
            raise ValueError(f"Base setting key must be a string, got {name}")
        combined = BaseSettings()
        combined.update(self[name + "_BASE"])
        combined.update(self[name])
        return combined

    def get_component_priority_dict_with_base(self, name: str) -> BaseSettings:
        if not isinstance(name, str):
            raise ValueError(f"Base setting key must be a string, got {name}")
        from .components import load_object

        normalized: dict[object, tuple[object, object]] = {}
        for key, value in dict(self[name + "_BASE"] or {}).items():
            try:
                identity = load_object(key)
            except (NameError, TypeError, ValueError):
                identity = key
            normalized[identity] = (key, value)
        for key, value in dict(self[name] or {}).items():
            try:
                identity = load_object(key)
            except (NameError, TypeError, ValueError):
                identity = key
            normalized[identity] = (key, value)
        return BaseSettings(
            {original: value for original, value in normalized.values() if value is not None}
        )

    def add_to_list(self, name: str, item: object) -> None:
        value = self.getlist(name)
        if item not in value:
            self.set(name, [*value, item], self.getpriority(name) or 0)

    def remove_from_list(self, name: str, item: object) -> None:
        value = self.getlist(name)
        if item not in value:
            raise ValueError(f"{item!r} not found in the {name} setting ({value!r}).")
        self.set(
            name,
            [value_item for value_item in value if value_item != item],
            self.getpriority(name) or 0,
        )

    def set_in_component_priority_dict(
        self,
        name: str,
        component: type,
        priority: int | None,
    ) -> None:
        from .components import load_object

        components = self.getdict(name)
        for reference in tuple(components):
            if isinstance(reference, str) and load_object(reference) == component:
                del components[reference]
        components[component] = priority
        self.set(name, components, self.getpriority(name) or 0)

    def setdefault_in_component_priority_dict(
        self,
        name: str,
        component: type,
        priority: int | None,
    ) -> None:
        from .components import load_object

        components = self.getdict(name)
        if any(load_object(reference) == component for reference in components):
            return
        components[component] = priority
        self.set(name, components, self.getpriority(name) or 0)

    def replace_in_component_priority_dict(
        self,
        name: str,
        old_component: type,
        new_component: type,
        priority: int | None = None,
    ) -> None:
        from .components import load_object

        components = self.getdict(name)
        old_priority = None
        for reference in tuple(components):
            if load_object(reference) != old_component:
                continue
            old_priority = components.pop(reference)
            if old_priority is None:
                break
        if old_priority is None:
            raise KeyError(f"{old_component} not found in the {name} setting ({components!r}).")
        components[new_component] = old_priority if priority is None else priority
        self.set(name, components, self.getpriority(name) or 0)

    def __getitem__(self, name: str) -> object:
        attribute = self.attributes.get(name)
        return None if attribute is None else attribute.value

    def __setitem__(self, name: str, value: object) -> None:
        self.set(name, value)

    def __delitem__(self, name: str) -> None:
        self._assert_mutability()
        del self.attributes[name]

    def __iter__(self) -> Iterator[str]:
        return iter(self.attributes)

    def __len__(self) -> int:
        return len(self.attributes)

    def __contains__(self, name: object) -> bool:
        return name in self.attributes

    def copy(self) -> BaseSettings:
        return copy.deepcopy(self)

    def frozencopy(self) -> BaseSettings:
        copied = self.copy()
        copied.freeze()
        return copied

    def copy_to_dict(self) -> dict[str, Any]:
        return self._to_dict()

    def _to_dict(self) -> dict[str, Any]:
        return {
            str(name): (
                value._to_dict() if isinstance(value, BaseSettings) else copy.deepcopy(value)
            )
            for name, value in self.items()
        }

    def pop(self, name: str, default: object = ...) -> object:
        if name not in self.attributes:
            if default is ...:
                raise KeyError(name)
            return default
        value = self.attributes[name].value
        del self[name]
        return value


class Settings(BaseSettings):
    def __init__(
        self,
        values: (
            BaseSettings | Mapping[str, object] | Iterable[tuple[str, object]] | str | None
        ) = None,
        priority: int | str = "project",
    ) -> None:
        super().__init__()
        self.update(DEFAULT_SETTINGS, priority="default")
        for name, value in tuple(self.items()):
            if isinstance(value, dict):
                self.set(name, BaseSettings(value, "default"), "default")
        self.update(values, priority)

    def copy(self) -> Settings:
        return copy.deepcopy(self)

    def frozencopy(self) -> Settings:
        copied = self.copy()
        copied.freeze()
        return copied
