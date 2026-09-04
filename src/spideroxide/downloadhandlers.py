from __future__ import annotations

import asyncio
import ftplib
import inspect
import logging
import os
import re
import warnings
from collections.abc import Mapping
from io import BytesIO
from pathlib import Path
from typing import Protocol
from urllib.parse import unquote, urlsplit, urlunsplit

from w3lib.url import file_uri_to_path, parse_data_uri

from .components import build_component, load_object
from .downloader import Downloader, _response_type, create_downloader
from .exceptions import NotConfigured, NotSupported
from .headers import Headers
from .http import Request, Response, TextResponse, _urljoin
from .utils import maybe_await

logger = logging.getLogger(__name__)
_FTP_CODE = re.compile(r"\d{3}")


class DownloadHandler(Protocol):
    lazy: bool

    async def download_request(self, request: Request) -> Response: ...

    async def close(self) -> None: ...


class BaseDownloadHandler:
    lazy = False

    def __init__(self, crawler: object) -> None:
        self.crawler = crawler

    @classmethod
    def from_crawler(cls, crawler: object) -> BaseDownloadHandler:
        return cls(crawler)

    async def close(self) -> None:
        return None


class HTTPDownloadHandler(BaseDownloadHandler):
    def __init__(self, crawler: object) -> None:
        super().__init__(crawler)
        self.downloader: Downloader = create_downloader(crawler.settings)  # type: ignore[attr-defined]

    async def download_request(self, request: Request) -> Response:
        return await self.downloader.fetch(request)

    async def close(self) -> None:
        close = getattr(self.downloader, "close", None)
        if close is not None:
            await maybe_await(close())


def _response_for(
    request: Request,
    body: bytes,
    *,
    headers: Headers | None = None,
    status: int = 200,
    encoding: str | None = None,
) -> Response:
    response_headers = headers or Headers()
    response_type = _response_type(response_headers, url=request.url, body=body)
    kwargs: dict[str, object] = {
        "url": request.url,
        "status": status,
        "headers": response_headers,
        "body": body,
        "request": request,
    }
    if encoding is not None and issubclass(response_type, TextResponse):
        kwargs["encoding"] = encoding
    return response_type(**kwargs)


class DataURIDownloadHandler(BaseDownloadHandler):
    async def download_request(self, request: Request) -> Response:
        uri = parse_data_uri(request.url)
        type_headers = Headers({"Content-Type": uri.media_type})
        response_type = _response_type(type_headers, url=request.url, body=uri.data)
        encoding = None
        if issubclass(response_type, TextResponse):
            encoding = uri.media_type_parameters.get("charset")
        kwargs: dict[str, object] = {
            "url": request.url,
            "body": uri.data,
            "request": request,
        }
        if encoding is not None:
            kwargs["encoding"] = encoding
        return response_type(**kwargs)


class FileDownloadHandler(BaseDownloadHandler):
    async def download_request(self, request: Request) -> Response:
        filepath = file_uri_to_path(request.url)
        body = await asyncio.to_thread(Path(filepath).read_bytes)
        return _response_for(request, body)


class FTPDownloadHandler(BaseDownloadHandler):
    CODE_MAPPING = {"550": 404, "default": 503}

    def __init__(self, crawler: object) -> None:
        super().__init__(crawler)
        settings = crawler.settings  # type: ignore[attr-defined]
        self.default_user = str(settings.get("FTP_USER", "anonymous"))
        self.default_password = str(settings.get("FTP_PASSWORD", "guest"))
        self.passive_mode = settings.getbool("FTP_PASSIVE_MODE", True)

    async def download_request(self, request: Request) -> Response:
        parsed = urlsplit(request.url)
        if parsed.hostname is None:
            raise ValueError(f"FTP URL must include a hostname: {request.url!r}")
        try:
            body, local_filename, size = await asyncio.to_thread(
                self._download,
                parsed.hostname,
                parsed.port or 21,
                unquote(parsed.path),
                str(request.meta.get("ftp_user", self.default_user)),
                str(request.meta.get("ftp_password", self.default_password)),
                bool(request.meta.get("ftp_passive", self.passive_mode)),
                request.meta.get("ftp_local_filename"),
            )
        except ftplib.Error as error:
            message = str(error)
            match = _FTP_CODE.search(message)
            if match is None:
                raise
            status = self.CODE_MAPPING.get(match.group(), self.CODE_MAPPING["default"])
            return Response(
                url=request.url,
                status=status,
                body=message.encode(),
                request=request,
            )
        headers = Headers({"Local Filename": local_filename or b"", "Size": size})
        return _response_for(request, body, headers=headers)

    @staticmethod
    def _download(
        hostname: str,
        port: int,
        remote_path: str,
        user: str,
        password: str,
        passive: bool,
        local_filename: object,
    ) -> tuple[bytes, bytes | None, int]:
        ftp = ftplib.FTP()
        output: BytesIO | object
        encoded_filename: bytes | None = None
        if local_filename is None:
            output = BytesIO()
        else:
            path = os.fsdecode(local_filename)
            output = Path(path).open("wb")
            encoded_filename = os.fsencode(path)
        size = 0
        connected = False

        def write(chunk: bytes) -> None:
            nonlocal size
            output.write(chunk)  # type: ignore[union-attr]
            size += len(chunk)

        try:
            ftp.connect(hostname, port)
            connected = True
            ftp.login(user, password)
            ftp.set_pasv(passive)
            ftp.retrbinary(f"RETR {remote_path}", write)
        finally:
            if local_filename is not None:
                output.close()  # type: ignore[union-attr]
            if connected:
                try:
                    ftp.quit()
                except (AttributeError, EOFError, OSError, ftplib.Error):
                    ftp.close()
            else:
                ftp.close()
        if encoded_filename is not None:
            return encoded_filename, encoded_filename, size
        assert isinstance(output, BytesIO)
        return output.getvalue(), None, size


class S3DownloadHandler(BaseDownloadHandler):
    lazy = True

    def __init__(self, crawler: object) -> None:
        try:
            import botocore.auth
            import botocore.credentials
        except ImportError as error:
            raise NotConfigured("missing botocore library") from error

        super().__init__(crawler)
        settings = crawler.settings  # type: ignore[attr-defined]
        access_key = settings.get("AWS_ACCESS_KEY_ID")
        secret_key = settings.get("AWS_SECRET_ACCESS_KEY")
        session_token = settings.get("AWS_SESSION_TOKEN")
        self._signer: object | None = None
        if access_key or secret_key:
            if not access_key or not secret_key:
                raise NotConfigured("both AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY are required")
            signer_type = botocore.auth.AUTH_TYPE_MAPS["s3"]
            credentials = botocore.credentials.Credentials(access_key, secret_key, session_token)
            self._signer = signer_type(credentials)

        handlers = _handler_mapping(settings)
        https_reference = handlers.get("https")
        if https_reference is None:
            raise NotConfigured("HTTPS download handler is disabled")
        https_component = (
            load_object(https_reference) if isinstance(https_reference, str) else https_reference
        )
        if not hasattr(https_component, "download_request"):
            raise TypeError("HTTPS download handler must define download_request()")
        self._http_handler = build_component(https_component, crawler)
        self._http_error: TypeError | None = None
        if self._http_handler is None or not hasattr(self._http_handler, "download_request"):
            self._http_error = TypeError("HTTPS download handler must define download_request()")

    async def download_request(self, request: Request) -> Response:
        if self._http_error is not None:
            raise self._http_error
        parsed = urlsplit(request.url)
        if parsed.hostname is None:
            raise ValueError(f"S3 URL must include a bucket: {request.url!r}")
        scheme = "http" if request.meta.get("is_secure") is False else "https"
        path = parsed.path + (f"?{parsed.query}" if parsed.query else "")
        endpoint = request.meta.get("_s3_endpoint")
        if endpoint is None:
            endpoint = f"{parsed.hostname}.s3.amazonaws.com"
        elif not isinstance(endpoint, str) or not endpoint:
            raise ValueError("request meta _s3_endpoint must be a non-empty string")
        url = f"{scheme}://{endpoint}{path}"
        if self._signer is None:
            http_request = request.replace(url=url)
        else:
            import botocore.awsrequest

            aws_request = botocore.awsrequest.AWSRequest(
                method=request.method,
                url=f"{scheme}://s3.amazonaws.com/{parsed.hostname}{path}",
                headers=dict(request.headers.to_http_pairs()),
                data=request.body,
            )
            self._signer.add_auth(aws_request)  # type: ignore[attr-defined]
            http_request = request.replace(url=url, headers=aws_request.headers.items())
        response = await maybe_await(self._http_handler.download_request(http_request))
        if "download_latency" in http_request.meta:
            request.meta["download_latency"] = http_request.meta["download_latency"]
        headers = response.headers.copy()
        location = headers.get("Location")
        if location is not None:
            location_text = location.decode("latin-1").strip()
            headers["Location"] = self._public_location(
                request.url,
                location_text,
            )
            redirect_endpoint = self._endpoint_for_location(location_text)
            if redirect_endpoint is not None:
                request.meta["_s3_endpoint"] = redirect_endpoint
        return response.replace(url=request.url, headers=headers, request=request)

    @staticmethod
    def _endpoint_for_location(location: str) -> str | None:
        parsed = urlsplit(location)
        hostname = parsed.hostname or ""
        marker = ".s3."
        if parsed.scheme in {"http", "https"} and marker in hostname:
            bucket, suffix = hostname.split(marker, 1)
            if bucket and suffix.endswith("amazonaws.com"):
                return hostname
        return None

    @classmethod
    def _public_location(cls, request_url: str, location: str) -> str:
        parsed = urlsplit(location)
        if not parsed.scheme:
            return _urljoin(request_url, location)
        endpoint = cls._endpoint_for_location(location)
        if endpoint is not None:
            bucket = endpoint.split(".s3.", 1)[0]
            return urlunsplit(("s3", bucket, parsed.path, parsed.query, parsed.fragment))
        return location

    async def close(self) -> None:
        close = getattr(self._http_handler, "close", None)
        if close is not None:
            await maybe_await(close())


def _handler_mapping(settings: object) -> dict[str, object]:
    base = settings.get("DOWNLOAD_HANDLERS_BASE", {})  # type: ignore[attr-defined]
    custom = settings.get("DOWNLOAD_HANDLERS", {})  # type: ignore[attr-defined]
    if not isinstance(base, Mapping) or not isinstance(custom, Mapping):
        raise TypeError("DOWNLOAD_HANDLERS settings must be mappings")
    merged = {str(scheme).lower(): reference for scheme, reference in base.items()}
    merged.update({str(scheme).lower(): reference for scheme, reference in custom.items()})
    return {scheme: reference for scheme, reference in merged.items() if reference is not None}


class DownloadHandlers:
    def __init__(self, crawler: object) -> None:
        self.crawler = crawler
        self._schemes = _handler_mapping(crawler.settings)  # type: ignore[attr-defined]
        self._handlers: dict[str, object] = {}
        self._cleanup_handlers: list[object] = []
        self._not_configured: dict[str, str] = {}
        self._legacy_handlers: set[str] = set()
        self._closed = False
        for scheme in self._schemes:
            self._load_handler(scheme, skip_lazy=True)

    @classmethod
    def from_crawler(cls, crawler: object) -> DownloadHandlers:
        return cls(crawler)

    def _get_handler(self, scheme: str) -> object | None:
        if scheme in self._handlers:
            return self._handlers[scheme]
        if scheme in self._not_configured:
            return None
        if scheme not in self._schemes:
            self._not_configured[scheme] = "no handler available for that scheme"
            return None
        return self._load_handler(scheme)

    def _load_handler(self, scheme: str, *, skip_lazy: bool = False) -> object | None:
        reference = self._schemes[scheme]
        component: object | None = None
        try:
            component = load_object(reference) if isinstance(reference, str) else reference
            if not hasattr(component, "download_request"):
                raise TypeError("download handler must define download_request()")
            if skip_lazy:
                if not hasattr(component, "lazy"):
                    warnings.warn(
                        f"{component!r} does not define lazy; defaulting to lazy loading",
                        DeprecationWarning,
                        stacklevel=2,
                    )
                if getattr(component, "lazy", True):
                    return None
            handler = build_component(component, self.crawler)
            if handler is None or not hasattr(handler, "download_request"):
                if handler is not None and hasattr(handler, "close"):
                    self._cleanup_handlers.append(handler)
                raise TypeError("download handler must define download_request()")
        except NotConfigured as error:
            self._not_configured[scheme] = str(error)
            return None
        except Exception as error:
            if scheme in {"http", "https"} and component is HTTPDownloadHandler:
                raise
            logger.error(
                "Loading %r for scheme %r",
                reference,
                scheme,
                exc_info=True,
            )
            self._not_configured[scheme] = str(error)
            return None
        self._handlers[scheme] = handler
        if not inspect.iscoroutinefunction(handler.download_request):
            warnings.warn(
                f"{handler.download_request!r} is not asynchronous",
                DeprecationWarning,
                stacklevel=2,
            )
            self._legacy_handlers.add(scheme)
        return handler

    async def fetch(self, request: Request) -> Response:
        if self._closed:
            raise RuntimeError("download handlers are closed")
        scheme = urlsplit(request.url).scheme.lower()
        handler = self._get_handler(scheme)
        if handler is None:
            reason = self._not_configured[scheme]
            raise NotSupported(f"Unsupported URL scheme '{scheme}': {reason}")
        method = handler.download_request
        request.meta.pop("download_latency", None)
        started = asyncio.get_running_loop().time()
        try:
            if scheme in self._legacy_handlers and "spider" in inspect.signature(method).parameters:
                result = await maybe_await(method(request, self.crawler.spider))
            else:
                result = await maybe_await(method(request))
        finally:
            if "download_latency" not in request.meta:
                request.meta["download_latency"] = asyncio.get_running_loop().time() - started
        if not isinstance(result, Response):
            raise TypeError("download_request must return a Response")
        return result if result.request is not None else result.replace(request=request)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        first_error: Exception | None = None
        seen: set[int] = set()
        for handler in (*self._handlers.values(), *self._cleanup_handlers):
            if id(handler) in seen:
                continue
            seen.add(id(handler))
            close = getattr(handler, "close", None)
            if close is not None:
                try:
                    await maybe_await(close())
                except Exception as error:
                    if first_error is None:
                        first_error = error
                    else:
                        logger.exception("Error closing download handler")
        if first_error is not None:
            raise first_error
