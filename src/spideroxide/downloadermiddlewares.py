from __future__ import annotations

import base64
import gzip
import io
import logging
import re
import zlib
from http import HTTPStatus
from urllib.parse import urlsplit

from . import signals
from .exceptions import IgnoreRequest, NotConfigured
from .http import Request, Response

logger = logging.getLogger(__name__)

ACCEPTED_ENCODINGS = [b"gzip", b"deflate"]
REASON_PHRASE_OVERRIDES: dict[int, str | None] = {
    102: None,
    103: None,
    205: "Reset Content.",
    208: None,
    226: None,
    408: "Request Time-out",
    413: "Request Entity Too Large",
    414: "Request-URI Too Long",
    416: "Requested Range not satisfiable",
    418: "I'm a teapot",
    421: None,
    422: None,
    423: None,
    424: None,
    425: None,
    426: None,
    428: None,
    429: None,
    431: None,
    451: None,
    504: "Gateway Time-out",
    505: "HTTP Version not supported",
    506: None,
    507: "Insufficient Storage Space",
    508: None,
    511: None,
}
try:
    import brotli
except ImportError:  # pragma: no cover - optional dependency
    brotli = None
else:
    ACCEPTED_ENCODINGS.append(b"br")

try:
    import zstandard
except ImportError:  # pragma: no cover - optional dependency
    zstandard = None
else:
    ACCEPTED_ENCODINGS.append(b"zstd")


def _basic_auth_header(username: object, password: object) -> bytes:
    credentials = f"{username}:{password}".encode("latin-1")
    return b"Basic " + base64.b64encode(credentials)


def _request_size(request: Request) -> int:
    parsed = urlsplit(request.url)
    target = parsed.path or "/"
    if parsed.query:
        target += f"?{parsed.query}"
    headers = request.headers.copy()
    if "Host" not in headers and parsed.hostname:
        headers["Host"] = parsed.hostname
    size = len(f"{request.method} {target} HTTP/1.1\r\n".encode("latin-1"))
    for name in headers:
        for value in headers.getlist(name):
            size += len(name.encode("latin-1")) + 2 + len(value) + 2
    return size + 2 + len(request.body)


def _response_size(response: Response) -> int:
    override = REASON_PHRASE_OVERRIDES.get(response.status, ...)
    if override is not ...:
        reason = override or ""
    else:
        try:
            reason = HTTPStatus(response.status).phrase
        except ValueError:
            reason = ""
    size = len(reason.encode("latin-1")) + 15
    header_size = 0
    for name in response.headers:
        for value in response.headers.getlist(name):
            header_size += len(name.encode("latin-1")) + 2 + len(value)
    header_size += 2 * max(len(response.headers) - 1, 0)
    return size + header_size + 4 + len(response.body)


class OffsiteMiddleware:
    def __init__(self, crawler: object) -> None:
        self.crawler = crawler
        self.stats = crawler.stats  # type: ignore[attr-defined]
        crawler.signals.connect(self.spider_opened, signal=signals.spider_opened)  # type: ignore[attr-defined]
        crawler.signals.connect(  # type: ignore[attr-defined]
            self.request_scheduled,
            signal=signals.request_scheduled,
        )
        self.host_regex = re.compile("")
        self.domains_seen: set[str] = set()

    @classmethod
    def from_crawler(cls, crawler: object) -> OffsiteMiddleware:
        return cls(crawler)

    def spider_opened(self, spider: object) -> None:
        self.host_regex = self.get_host_regex(spider)

    def request_scheduled(self, request: Request, spider: object) -> None:
        self.process_request(request, spider)

    def process_request(self, request: Request, spider: object) -> None:
        if request.dont_filter or request.meta.get("allow_offsite") or self.should_follow(request):
            return
        domain = urlsplit(request.url).hostname
        if domain and domain not in self.domains_seen:
            self.domains_seen.add(domain)
            self.stats.inc_value("offsite/domains")
            logger.debug("Filtered offsite request to %r: %s", domain, request.url)
        self.stats.inc_value("offsite/filtered")
        raise IgnoreRequest("Filtered offsite request")

    def should_follow(self, request: Request) -> bool:
        host = urlsplit(request.url).hostname or ""
        return bool(self.host_regex.search(host))

    @staticmethod
    def get_host_regex(spider: object) -> re.Pattern[str]:
        allowed_domains = getattr(spider, "allowed_domains", None)
        if not allowed_domains:
            return re.compile("")
        domains = []
        for domain in allowed_domains:
            if not domain:
                continue
            parsed = urlsplit(str(domain))
            if parsed.scheme or parsed.port is not None:
                logger.warning(
                    "allowed_domains accepts domains only, not URLs or ports: %r",
                    domain,
                )
                continue
            domains.append(re.escape(str(domain)))
        return re.compile(rf"^(.*\.)?({'|'.join(domains)})$") if domains else re.compile(r"^$")


class HttpAuthMiddleware:
    def __init__(self, crawler: object) -> None:
        self.crawler = crawler
        settings = crawler.settings  # type: ignore[attr-defined]
        self.username = settings.get("HTTPAUTH_USER", "")
        self.password = settings.get("HTTPAUTH_PASS", "")
        self.domain = settings.get("HTTPAUTH_DOMAIN")
        if (self.username or self.password) and (settings.getpriority("HTTPAUTH_DOMAIN") or 0) <= 0:
            raise ValueError(
                "HTTPAUTH_DOMAIN must be configured when HTTPAUTH_USER or "
                "HTTPAUTH_PASS is set; use None to allow every domain"
            )
        self.auth = self._auth_header()
        crawler.signals.connect(self.spider_opened, signal=signals.spider_opened)  # type: ignore[attr-defined]

    @classmethod
    def from_crawler(cls, crawler: object) -> HttpAuthMiddleware:
        return cls(crawler)

    def spider_opened(self, spider: object) -> None:
        self.username = getattr(spider, "http_user", self.username)
        self.password = getattr(spider, "http_pass", self.password)
        self.domain = getattr(spider, "http_auth_domain", self.domain)
        self.auth = self._auth_header()

    def process_request(self, request: Request, spider: object) -> None:
        if "Authorization" in request.headers:
            return
        username = request.meta.get("http_user", "")
        password = request.meta.get("http_pass", "")
        if username or password:
            domain = request.meta.get("http_auth_domain")
            if self._request_matches_domain(request, domain):
                request.headers["Authorization"] = _basic_auth_header(
                    username,
                    password,
                )
            return
        auth = self.auth
        domain = self.domain
        if auth is None:
            return
        if self._request_matches_domain(request, domain):
            request.headers["Authorization"] = auth

    def _auth_header(self) -> bytes | None:
        if not self.username and not self.password:
            return None
        return _basic_auth_header(self.username, self.password)

    @staticmethod
    def _request_matches_domain(request: Request, domain: object) -> bool:
        if not domain:
            return True
        request_domain = urlsplit(request.url).hostname
        normalized = str(domain).lower()
        return request_domain == normalized or (
            request_domain is not None and request_domain.endswith(f".{normalized}")
        )


class DownloadTimeoutMiddleware:
    def __init__(self, timeout: float) -> None:
        self._timeout = timeout

    @classmethod
    def from_crawler(cls, crawler: object) -> DownloadTimeoutMiddleware:
        return cls(crawler.settings.getfloat("DOWNLOAD_TIMEOUT", 180.0))  # type: ignore[attr-defined]

    def process_request(self, request: Request, spider: object) -> None:
        timeout = getattr(spider, "download_timeout", self._timeout)
        request.meta.setdefault("download_timeout", timeout)


class DefaultHeadersMiddleware:
    def __init__(self, headers: dict[str, object]) -> None:
        self._headers = headers

    @classmethod
    def from_crawler(cls, crawler: object) -> DefaultHeadersMiddleware:
        return cls(crawler.settings.getdict("DEFAULT_REQUEST_HEADERS", {}))  # type: ignore[attr-defined]

    def process_request(self, request: Request, spider: object) -> None:
        for name, value in self._headers.items():
            request.headers.setdefault(name, value)


class UserAgentMiddleware:
    def __init__(self, user_agent: object) -> None:
        self.user_agent = user_agent

    @classmethod
    def from_crawler(cls, crawler: object) -> UserAgentMiddleware:
        return cls(crawler.settings.get("USER_AGENT"))  # type: ignore[attr-defined]

    def process_request(self, request: Request, spider: object) -> None:
        user_agent = getattr(spider, "user_agent", self.user_agent)
        if user_agent:
            request.headers.setdefault("User-Agent", user_agent)


class HttpCompressionMiddleware:
    def __init__(self, crawler: object) -> None:
        self.crawler = crawler
        self.stats = crawler.stats  # type: ignore[attr-defined]
        self.max_size = crawler.settings.getint("DOWNLOAD_MAXSIZE")  # type: ignore[attr-defined]
        self.warn_size = crawler.settings.getint("DOWNLOAD_WARNSIZE")  # type: ignore[attr-defined]
        crawler.signals.connect(self.spider_opened, signal=signals.spider_opened)  # type: ignore[attr-defined]

    @classmethod
    def from_crawler(cls, crawler: object) -> HttpCompressionMiddleware:
        if not crawler.settings.getbool("COMPRESSION_ENABLED", True):  # type: ignore[attr-defined]
            raise NotConfigured
        return cls(crawler)

    def spider_opened(self, spider: object) -> None:
        self.max_size = getattr(spider, "download_maxsize", self.max_size)
        self.warn_size = getattr(spider, "download_warnsize", self.warn_size)

    def process_request(self, request: Request, spider: object) -> None:
        request.headers.setdefault("Accept-Encoding", b", ".join(ACCEPTED_ENCODINGS))

    def process_response(
        self,
        request: Request,
        response: Response,
        spider: object,
    ) -> Response:
        if request.method == "HEAD":
            return response
        encodings = [
            value.strip().lower()
            for header in response.headers.getlist("Content-Encoding")
            for value in header.split(b",")
            if value.strip()
        ]
        if not encodings:
            return response
        supported = {*ACCEPTED_ENCODINGS, b"x-gzip"}
        to_decode = []
        while encodings and encodings[-1] in supported:
            to_decode.append(encodings.pop())
        body = response.body
        max_size = int(request.meta.get("download_maxsize", self.max_size))
        warn_size = int(request.meta.get("download_warnsize", self.warn_size))
        for encoding in to_decode:
            body = self._decode(body, encoding, max_size)
            if max_size and len(body) > max_size:
                raise IgnoreRequest(
                    f"Ignored response {response.url!r}: decompressed body exceeds "
                    f"DOWNLOAD_MAXSIZE ({max_size} B)"
                )
        if not to_decode:
            logger.warning(
                "%s cannot decode response from unsupported encoding(s) %r",
                type(self).__name__,
                encodings,
            )
            return response
        if len(response.body) < warn_size <= len(body):
            logger.warning(
                "%r body size after decompression (%d B) exceeds DOWNLOAD_WARNSIZE (%d B)",
                response,
                len(body),
                warn_size,
            )
        headers = response.headers.copy()
        if encodings:
            headers["Content-Encoding"] = b", ".join(encodings)
        else:
            headers.pop("Content-Encoding", None)
        self.stats.inc_value("httpcompression/response_bytes", len(body))
        self.stats.inc_value("httpcompression/response_count")
        from .downloader import _response_type
        from .http import TextResponse

        response_type = _response_type(headers, url=response.url, body=body)
        changes: dict[str, object] = {"headers": headers, "body": body}
        if issubclass(response_type, TextResponse):
            changes["encoding"] = None
        return response.replace(cls=response_type, **changes)

    @staticmethod
    def _decode(body: bytes, encoding: bytes, max_size: int) -> bytes:
        if encoding in {b"gzip", b"x-gzip"}:
            if not max_size:
                return gzip.decompress(body)
            with gzip.GzipFile(fileobj=io.BytesIO(body)) as compressed:
                return compressed.read(max_size + 1)
        if encoding == b"deflate":
            try:
                return HttpCompressionMiddleware._inflate(body, max_size, zlib.MAX_WBITS)
            except zlib.error:
                return HttpCompressionMiddleware._inflate(body, max_size, -zlib.MAX_WBITS)
        if encoding == b"br" and brotli is not None:
            if max_size:
                decompressor = brotli.Decompressor()
                decoded = bytearray()
                for offset in range(0, len(body), 1024):
                    decoded.extend(decompressor.process(body[offset : offset + 1024]))
                    if len(decoded) > max_size:
                        return bytes(decoded)
                return bytes(decoded)
            return brotli.decompress(body)
        if encoding == b"zstd" and zstandard is not None:
            if max_size:
                with zstandard.ZstdDecompressor().stream_reader(io.BytesIO(body)) as reader:
                    return reader.read(max_size + 1)
            return zstandard.ZstdDecompressor().decompress(body)
        return body

    @staticmethod
    def _inflate(body: bytes, max_size: int, window_bits: int) -> bytes:
        if not max_size:
            return zlib.decompress(body, window_bits)
        decompressor = zlib.decompressobj(window_bits)
        decoded = decompressor.decompress(body, max_size + 1)
        if len(decoded) > max_size or decompressor.unconsumed_tail:
            return decoded
        remaining = max_size + 1 - len(decoded)
        return decoded + decompressor.flush(remaining)


class DownloaderStatsMiddleware:
    def __init__(self, crawler: object) -> None:
        self.crawler = crawler
        self.stats = crawler.stats  # type: ignore[attr-defined]
        self.native_runtime = getattr(crawler, "native_policy_runtime", None)

    @classmethod
    def from_crawler(cls, crawler: object) -> DownloaderStatsMiddleware:
        if not crawler.settings.getbool("DOWNLOADER_STATS", True):  # type: ignore[attr-defined]
            raise NotConfigured
        return cls(crawler)

    def process_request(self, request: Request, spider: object) -> None:
        if self.native_runtime is not None:
            self.native_runtime.record_request(request.method)
            self._sync_native_stats()
        else:
            self.stats.inc_value("downloader/request_count")
            self.stats.inc_value(f"downloader/request_method_count/{request.method}")
        self.stats.inc_value("downloader/request_bytes", _request_size(request))

    def process_response(
        self,
        request: Request,
        response: Response,
        spider: object,
    ) -> Response:
        if self.native_runtime is not None:
            self.native_runtime.record_response(response.status)
            self._sync_native_stats()
        else:
            self.stats.inc_value("downloader/response_count")
            self.stats.inc_value(f"downloader/response_status_count/{response.status}")
        self.stats.inc_value("downloader/response_bytes", _response_size(response))
        return response

    def process_exception(
        self,
        request: Request,
        exception: Exception,
        spider: object,
    ) -> None:
        exception_name = self._exception_name(exception)
        if self.native_runtime is not None:
            self.native_runtime.record_exception(exception_name)
            self._sync_native_stats()
        else:
            self.stats.inc_value("downloader/exception_count")
            self.stats.inc_value(f"downloader/exception_type_count/{exception_name}")
        return None

    @staticmethod
    def _exception_name(exception: Exception) -> str:
        exception_type = type(exception)
        return f"{exception_type.__module__}.{exception_type.__qualname__}"

    def _sync_native_stats(self) -> None:
        from .native_policy import sync_stats

        sync_stats(self.crawler)
