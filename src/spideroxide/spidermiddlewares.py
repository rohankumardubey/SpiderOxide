from __future__ import annotations

import logging
import warnings
from abc import ABC, abstractmethod
from collections.abc import AsyncIterable, AsyncIterator, Iterable, Iterator
from urllib.parse import urlsplit, urlunsplit

from .components import load_object
from .exceptions import NotConfigured
from .http import Request, Response
from .settings import Settings

logger = logging.getLogger(__name__)

LOCAL_SCHEMES = ("about", "blob", "data", "filesystem")
DEFAULT_PORTS = {"http": 80, "https": 443}


def _strip_referrer_url(url: str, *, origin_only: bool) -> str:
    parsed = urlsplit(url)
    hostname = parsed.hostname or ""
    if ":" in hostname and not hostname.startswith("["):
        hostname = f"[{hostname}]"
    port = parsed.port
    if port is not None and port != DEFAULT_PORTS.get(parsed.scheme):
        hostname = f"{hostname}:{port}"
    return urlunsplit(
        (
            parsed.scheme,
            hostname,
            "/" if origin_only else parsed.path,
            "" if origin_only else parsed.query,
            "",
        )
    )


class BaseSpiderMiddleware:
    _legacy_start_compatible = True

    def __init__(self, crawler: object | None = None) -> None:
        self.crawler = crawler

    def process_spider_output(
        self,
        response: Response,
        result: Iterable[object],
        spider: object,
    ) -> Iterator[object]:
        for output in result:
            if isinstance(output, Request):
                processed = self.get_processed_request(output, response)
                if processed is not None:
                    yield processed
            else:
                processed_item = self.get_processed_item(output, response)
                if processed_item is not None:
                    yield processed_item

    async def process_spider_output_async(
        self,
        response: Response,
        result: AsyncIterable[object],
        spider: object,
    ) -> AsyncIterator[object]:
        async for output in result:
            if isinstance(output, Request):
                processed = self.get_processed_request(output, response)
                if processed is not None:
                    yield processed
            else:
                processed_item = self.get_processed_item(output, response)
                if processed_item is not None:
                    yield processed_item

    async def process_start(
        self,
        start: AsyncIterable[object],
        spider: object,
    ) -> AsyncIterator[object]:
        async for output in start:
            if isinstance(output, Request):
                processed = self.get_processed_request(output, None)
                if processed is not None:
                    yield processed
            else:
                processed_item = self.get_processed_item(output, None)
                if processed_item is not None:
                    yield processed_item

    def get_processed_request(
        self,
        request: Request,
        response: Response | None,
    ) -> Request | None:
        return request

    def get_processed_item(
        self,
        item: object,
        response: Response | None,
    ) -> object | None:
        return item


class StartSpiderMiddleware(BaseSpiderMiddleware):
    def get_processed_request(
        self,
        request: Request,
        response: Response | None,
    ) -> Request | None:
        if response is None:
            request.meta.setdefault("is_start_request", True)
        return request


class HttpError(Exception):
    def __init__(self, response: Response, message: str = "Ignoring non-200 response") -> None:
        super().__init__(message)
        self.response = response


class HttpErrorMiddleware:
    def __init__(self, settings: Settings) -> None:
        self.handle_httpstatus_all = settings.getbool("HTTPERROR_ALLOW_ALL", False)
        self.handle_httpstatus_list = settings.getlist("HTTPERROR_ALLOWED_CODES", [])

    @classmethod
    def from_crawler(cls, crawler: object) -> HttpErrorMiddleware:
        middleware = cls(crawler.settings)  # type: ignore[attr-defined]
        middleware.crawler = crawler
        return middleware

    def process_spider_input(self, response: Response, spider: object) -> None:
        if 200 <= response.status < 300:
            return
        meta = response.meta
        if meta.get("handle_httpstatus_all", False):
            return
        if "handle_httpstatus_list" in meta:
            allowed_statuses = meta["handle_httpstatus_list"]
        elif self.handle_httpstatus_all:
            return
        else:
            allowed_statuses = getattr(
                spider,
                "handle_httpstatus_list",
                self.handle_httpstatus_list,
            )
        if response.status not in allowed_statuses:
            raise HttpError(response)

    def process_spider_exception(
        self,
        response: Response,
        exception: Exception,
        spider: object,
    ) -> Iterable[object] | None:
        if not isinstance(exception, HttpError):
            return None
        self.crawler.stats.inc_value("httperror/response_ignored_count")
        self.crawler.stats.inc_value(f"httperror/response_ignored_status_count/{response.status}")
        logger.info(
            "Ignoring response %r: HTTP status code is not handled or not allowed",
            response,
        )
        return ()


class ReferrerPolicy(ABC):
    NOREFERRER_SCHEMES = LOCAL_SCHEMES
    name: str

    @abstractmethod
    def referrer(self, response_url: str, request_url: str) -> str | None:
        raise NotImplementedError

    def stripped_referrer(self, url: str) -> str | None:
        if urlsplit(url).scheme in self.NOREFERRER_SCHEMES:
            return None
        return self.strip_url(url)

    def origin_referrer(self, url: str) -> str | None:
        if urlsplit(url).scheme in self.NOREFERRER_SCHEMES:
            return None
        return self.origin(url)

    def strip_url(self, url: str, origin_only: bool = False) -> str | None:
        if not url:
            return None
        return _strip_referrer_url(url, origin_only=origin_only)

    def origin(self, url: str) -> str | None:
        return self.strip_url(url, origin_only=True)

    @staticmethod
    def tls_protected(url: str) -> bool:
        return urlsplit(url).scheme in {"https", "ftps"}

    def potentially_trustworthy(self, url: str) -> bool:
        if urlsplit(url).scheme == "data":
            return False
        return self.tls_protected(url)


class NoReferrerPolicy(ReferrerPolicy):
    name = "no-referrer"

    def referrer(self, response_url: str, request_url: str) -> None:
        return None


class NoReferrerWhenDowngradePolicy(ReferrerPolicy):
    name = "no-referrer-when-downgrade"

    def referrer(self, response_url: str, request_url: str) -> str | None:
        if not self.tls_protected(response_url) or self.tls_protected(request_url):
            return self.stripped_referrer(response_url)
        return None


class SameOriginPolicy(ReferrerPolicy):
    name = "same-origin"

    def referrer(self, response_url: str, request_url: str) -> str | None:
        if self.origin(response_url) == self.origin(request_url):
            return self.stripped_referrer(response_url)
        return None


class OriginPolicy(ReferrerPolicy):
    name = "origin"

    def referrer(self, response_url: str, request_url: str) -> str | None:
        return self.origin_referrer(response_url)


class StrictOriginPolicy(ReferrerPolicy):
    name = "strict-origin"

    def referrer(self, response_url: str, request_url: str) -> str | None:
        if (
            self.tls_protected(response_url) and self.potentially_trustworthy(request_url)
        ) or not self.tls_protected(response_url):
            return self.origin_referrer(response_url)
        return None


class OriginWhenCrossOriginPolicy(ReferrerPolicy):
    name = "origin-when-cross-origin"

    def referrer(self, response_url: str, request_url: str) -> str | None:
        origin = self.origin(response_url)
        if origin == self.origin(request_url):
            return self.stripped_referrer(response_url)
        return origin


class StrictOriginWhenCrossOriginPolicy(ReferrerPolicy):
    name = "strict-origin-when-cross-origin"

    def referrer(self, response_url: str, request_url: str) -> str | None:
        origin = self.origin(response_url)
        if origin == self.origin(request_url):
            return self.stripped_referrer(response_url)
        if (
            self.tls_protected(response_url) and self.potentially_trustworthy(request_url)
        ) or not self.tls_protected(response_url):
            return self.origin_referrer(response_url)
        return None


class UnsafeUrlPolicy(ReferrerPolicy):
    name = "unsafe-url"

    def referrer(self, response_url: str, request_url: str) -> str | None:
        return self.stripped_referrer(response_url)


class DefaultReferrerPolicy(NoReferrerWhenDowngradePolicy):
    NOREFERRER_SCHEMES = (*LOCAL_SCHEMES, "file", "s3")
    name = "scrapy-default"


class RefererMiddleware(BaseSpiderMiddleware):
    def __init__(self, settings: Settings | None = None) -> None:
        self.default_policy: type[ReferrerPolicy] = DefaultReferrerPolicy
        self.policies = {
            policy.name: policy
            for policy in (
                NoReferrerPolicy,
                NoReferrerWhenDowngradePolicy,
                SameOriginPolicy,
                OriginPolicy,
                StrictOriginPolicy,
                OriginWhenCrossOriginPolicy,
                StrictOriginWhenCrossOriginPolicy,
                UnsafeUrlPolicy,
                DefaultReferrerPolicy,
            )
        }
        self.policies[""] = NoReferrerWhenDowngradePolicy
        if settings is None:
            return
        for name, reference in settings.getdict("REFERRER_POLICIES", {}).items():
            if reference is None:
                self.policies.pop(name, None)
            else:
                policy = load_object(reference) if isinstance(reference, str) else reference
                if not self._is_policy_class(policy):
                    raise TypeError(f"referrer policy {reference!r} is not a ReferrerPolicy")
                self.policies[name] = policy
        configured = self._load_policy_class(
            settings.get("REFERRER_POLICY", "scrapy-default"),
            allow_import_path=True,
        )
        assert configured is not None
        self.default_policy = configured

    @classmethod
    def from_crawler(cls, crawler: object) -> RefererMiddleware:
        if not crawler.settings.getbool("REFERER_ENABLED", True):  # type: ignore[attr-defined]
            raise NotConfigured
        return cls(crawler.settings)  # type: ignore[attr-defined]

    def policy(self, response: Response | str, request: Request) -> ReferrerPolicy:
        allow_import_path = True
        policy_name = request.meta.get("referrer_policy")
        if policy_name is None and isinstance(response, Response):
            header = response.headers.get("Referrer-Policy")
            if header is not None:
                policy_name = header.decode("latin-1")
                allow_import_path = False
        if policy_name is None:
            return self.default_policy()
        policy_class = self._load_policy_class(
            policy_name,
            warning_only=True,
            allow_import_path=allow_import_path,
        )
        return (policy_class or self.default_policy)()

    def _load_policy_class(
        self,
        policy: object,
        warning_only: bool = False,
        *,
        allow_import_path: bool = False,
    ) -> type[ReferrerPolicy] | None:
        if self._is_policy_class(policy):
            return policy
        if not isinstance(policy, str):
            message = f"Could not load referrer policy {policy!r}"
            if warning_only:
                warnings.warn(message, RuntimeWarning, stacklevel=2)
                return None
            raise RuntimeError(message)
        if allow_import_path and "." in policy:
            module_name, _, class_name = policy.rpartition(".")
            if module_name in {
                "scrapy.spidermiddlewares.referer",
                "spideroxide.spidermiddlewares",
            }:
                built_in = globals().get(class_name)
                if self._is_policy_class(built_in):
                    return built_in
            try:
                loaded = load_object(policy)
            except (ImportError, ValueError):
                pass
            else:
                if self._is_policy_class(loaded):
                    return loaded
        for policy_name in reversed([name.strip() for name in policy.lower().split(",")]):
            if policy_name in self.policies:
                return self.policies[policy_name]
        message = f"Could not load referrer policy {policy!r}"
        if warning_only:
            warnings.warn(message, RuntimeWarning, stacklevel=2)
            return None
        raise RuntimeError(message)

    @staticmethod
    def _is_policy_class(policy: object) -> bool:
        return isinstance(policy, type) and callable(getattr(policy, "referrer", None))

    def get_processed_request(
        self,
        request: Request,
        response: Response | None,
    ) -> Request | None:
        if response is None:
            return request
        referrer = self.policy(response, request).referrer(response.url, request.url)
        if referrer is not None:
            request.headers.setdefault("Referer", referrer)
        return request


class UrlLengthMiddleware(BaseSpiderMiddleware):
    def __init__(self, maxlength: int) -> None:
        self.maxlength = maxlength

    @classmethod
    def from_crawler(cls, crawler: object) -> UrlLengthMiddleware:
        maxlength = crawler.settings.getint("URLLENGTH_LIMIT", 2083)  # type: ignore[attr-defined]
        if not maxlength:
            raise NotConfigured
        middleware = cls(maxlength)
        middleware.crawler = crawler
        return middleware

    def get_processed_request(
        self,
        request: Request,
        response: Response | None,
    ) -> Request | None:
        if len(request.url) <= self.maxlength:
            return request
        logger.info(
            "Ignoring link (url length > %d): %s",
            self.maxlength,
            request.url,
        )
        self.crawler.stats.inc_value("urllength/request_ignored_count")
        return None


class MetaCopyDetectionMiddleware(BaseSpiderMiddleware):
    _INTERNAL_KEYS = frozenset(
        {
            "_auth_proxy",
            "_dont_cache",
            "_scheme_proxy",
            "cache_timestamp",
            "download_latency",
            "redirect_reasons",
            "redirect_times",
            "redirect_ttl",
            "redirect_urls",
            "retry_times",
        }
    )

    def __init__(self, crawler: object) -> None:
        super().__init__(crawler)
        skip = frozenset(crawler.settings.getlist("META_COPY_WARN_SKIP_KEYS", []))  # type: ignore[attr-defined]
        self._keys = self._INTERNAL_KEYS - skip
        self._warned = False

    @classmethod
    def from_crawler(cls, crawler: object) -> MetaCopyDetectionMiddleware:
        return cls(crawler)

    def get_processed_request(
        self,
        request: Request,
        response: Response | None,
    ) -> Request | None:
        if response is None or self._warned:
            return request
        found = self._keys & request.meta.keys()
        if found:
            logger.warning(
                "%s yielded a request containing internal meta keys that were likely "
                "copied from response.meta and should not be forwarded to new requests: %s. "
                "Source response: %r, target request: %r",
                type(self.crawler.spider).__name__,
                sorted(found),
                response,
                request,
            )
            self._warned = True
        return request
