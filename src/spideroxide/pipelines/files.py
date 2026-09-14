from __future__ import annotations

import asyncio
import base64
import functools
import hashlib
import logging
import mimetypes
import posixpath
import time
from collections.abc import Awaitable, Mapping
from contextlib import suppress
from datetime import datetime, timezone
from ftplib import FTP, error_perm
from io import BytesIO
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Protocol
from urllib.parse import unquote, urlparse

from itemadapter import ItemAdapter

from ..exceptions import NotConfigured
from ..http import Request, Response
from ..utils import maybe_await
from .media import FileInfo, MediaPipeline

if TYPE_CHECKING:
    from os import PathLike

    from ..crawler import Crawler

logger = logging.getLogger(__name__)


class FileException(Exception):
    """General media processing error."""


class FilesStoreProtocol(Protocol):
    def persist_file(
        self,
        path: str,
        buf: BytesIO,
        info: MediaPipeline.SpiderInfo,
        meta: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> object: ...

    def stat_file(
        self,
        path: str,
        info: MediaPipeline.SpiderInfo,
    ) -> object: ...


class FSFilesStore:
    def __init__(self, basedir: str | PathLike[str]) -> None:
        root = str(basedir)
        if "://" in root:
            root = root.split("://", 1)[1]
        from .._native import NativeMediaStore

        self.basedir = root
        self._store = NativeMediaStore(Path(root))

    def persist_file(
        self,
        path: str,
        buf: BytesIO,
        info: MediaPipeline.SpiderInfo,
        meta: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> str:
        del info, meta, headers
        return self._store.persist(path, buf.getvalue())

    def stat_file(self, path: str, info: MediaPipeline.SpiderInfo) -> dict[str, object]:
        del info
        result = self._store.stat(path)
        if result is None:
            return {}
        last_modified, checksum = result
        return {"last_modified": last_modified, "checksum": checksum}


def _remote_object_path(prefix: str, path: str) -> str:
    normalized_prefix = prefix.strip("/")
    return f"{normalized_prefix}/{path}" if normalized_prefix else path


class S3FilesStore:
    HEADERS: ClassVar[dict[str, str]] = {"Cache-Control": "max-age=172800"}
    _HEADER_ARGUMENTS: ClassVar[dict[str, str]] = {
        "cache-control": "CacheControl",
        "content-disposition": "ContentDisposition",
        "content-encoding": "ContentEncoding",
        "content-language": "ContentLanguage",
        "content-length": "ContentLength",
        "content-md5": "ContentMD5",
        "content-type": "ContentType",
        "expires": "Expires",
        "x-amz-grant-full-control": "GrantFullControl",
        "x-amz-grant-read": "GrantRead",
        "x-amz-grant-read-acp": "GrantReadACP",
        "x-amz-grant-write-acp": "GrantWriteACP",
        "x-amz-object-lock-legal-hold": "ObjectLockLegalHoldStatus",
        "x-amz-object-lock-mode": "ObjectLockMode",
        "x-amz-object-lock-retain-until-date": "ObjectLockRetainUntilDate",
        "x-amz-request-payer": "RequestPayer",
        "x-amz-server-side-encryption": "ServerSideEncryption",
        "x-amz-server-side-encryption-aws-kms-key-id": "SSEKMSKeyId",
        "x-amz-server-side-encryption-context": "SSEKMSEncryptionContext",
        "x-amz-server-side-encryption-customer-algorithm": "SSECustomerAlgorithm",
        "x-amz-server-side-encryption-customer-key": "SSECustomerKey",
        "x-amz-server-side-encryption-customer-key-md5": "SSECustomerKeyMD5",
        "x-amz-storage-class": "StorageClass",
        "x-amz-tagging": "Tagging",
        "x-amz-website-redirect-location": "WebsiteRedirectLocation",
    }

    def __init__(
        self,
        uri: str,
        *,
        access_key: object = None,
        secret_key: object = None,
        session_token: object = None,
        endpoint_url: object = None,
        region_name: object = None,
        use_ssl: object = None,
        verify: object = None,
        policy: object = "private",
    ) -> None:
        try:
            import boto3.session
        except ImportError:
            raise NotConfigured(
                "S3 media storage requires boto3; install spideroxide[s3]"
            ) from None
        parsed = urlparse(uri)
        if parsed.scheme != "s3" or not parsed.hostname:
            raise ValueError(f"Incorrect S3 media storage URI: {uri}")
        self.bucket = parsed.hostname
        self.prefix = parsed.path
        self.policy = str(policy) if policy else None
        self.s3_client = boto3.session.Session().client(
            "s3",
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            aws_session_token=session_token,
            endpoint_url=endpoint_url,
            region_name=region_name,
            use_ssl=use_ssl,
            verify=verify,
        )

    @classmethod
    def from_crawler(
        cls,
        crawler: Crawler,
        uri: str,
        *,
        media_name: str,
    ) -> S3FilesStore:
        settings = crawler.settings
        policy_setting = "IMAGES_STORE_S3_ACL" if media_name == "image" else "FILES_STORE_S3_ACL"
        return cls(
            uri,
            access_key=settings.get("AWS_ACCESS_KEY_ID"),
            secret_key=settings.get("AWS_SECRET_ACCESS_KEY"),
            session_token=settings.get("AWS_SESSION_TOKEN"),
            endpoint_url=settings.get("AWS_ENDPOINT_URL"),
            region_name=settings.get("AWS_REGION_NAME"),
            use_ssl=settings.get("AWS_USE_SSL"),
            verify=settings.get("AWS_VERIFY"),
            policy=settings.get(policy_setting, "private"),
        )

    def _key(self, path: str) -> str:
        return _remote_object_path(self.prefix, path)

    @classmethod
    def _header_arguments(cls, headers: Mapping[str, object]) -> dict[str, object]:
        arguments: dict[str, object] = {}
        for name, value in headers.items():
            try:
                argument_name = cls._HEADER_ARGUMENTS[name.lower()]
            except KeyError:
                raise TypeError(f'Header "{name}" is not supported by S3 media storage') from None
            arguments[argument_name] = value
        return arguments

    def _stat_file(self, path: str) -> dict[str, object]:
        from botocore.exceptions import ClientError

        try:
            result = self.s3_client.head_object(Bucket=self.bucket, Key=self._key(path))
        except ClientError as error:
            response = error.response
            error_code = str(response.get("Error", {}).get("Code", ""))
            status = response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            if error_code in {"404", "NoSuchKey", "NotFound"} or status == 404:
                return {}
            raise
        last_modified = result.get("LastModified")
        if not isinstance(last_modified, datetime):
            raise TypeError("S3 head_object response has no LastModified datetime")
        etag = result.get("ETag")
        return {
            "last_modified": last_modified.timestamp(),
            "checksum": str(etag).strip('"') if etag is not None else None,
        }

    async def stat_file(
        self,
        path: str,
        info: MediaPipeline.SpiderInfo,
    ) -> dict[str, object]:
        del info
        return await asyncio.to_thread(self._stat_file, path)

    async def persist_file(
        self,
        path: str,
        buf: BytesIO,
        info: MediaPipeline.SpiderInfo,
        meta: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        del info
        arguments: dict[str, object] = {
            "Bucket": self.bucket,
            "Key": self._key(path),
            "Body": buf.getvalue(),
            "Metadata": {name: str(value) for name, value in (meta or {}).items()},
        }
        if self.policy:
            arguments["ACL"] = self.policy
        arguments.update(self._header_arguments({**self.HEADERS, **(headers or {})}))
        await asyncio.to_thread(self.s3_client.put_object, **arguments)


class GCSFilesStore:
    CACHE_CONTROL = "max-age=172800"

    def __init__(
        self,
        uri: str,
        *,
        project_id: object = None,
        policy: object = None,
    ) -> None:
        try:
            from google.cloud.storage import Client
        except ImportError:
            raise NotConfigured(
                "GCS media storage requires google-cloud-storage; install spideroxide[gcs]"
            ) from None
        parsed = urlparse(uri)
        if parsed.scheme != "gs" or not parsed.hostname:
            raise ValueError(f"Incorrect GCS media storage URI: {uri}")
        self.bucket_name = parsed.hostname
        self.prefix = parsed.path
        self.policy = str(policy) if policy else None
        self.bucket = Client(project=project_id).bucket(self.bucket_name)

    @classmethod
    def from_crawler(
        cls,
        crawler: Crawler,
        uri: str,
        *,
        media_name: str,
    ) -> GCSFilesStore:
        policy_setting = "IMAGES_STORE_GCS_ACL" if media_name == "image" else "FILES_STORE_GCS_ACL"
        return cls(
            uri,
            project_id=crawler.settings.get("GCS_PROJECT_ID"),
            policy=crawler.settings.get(policy_setting) or None,
        )

    def _blob_path(self, path: str) -> str:
        return _remote_object_path(self.prefix, path)

    def _stat_file(self, path: str) -> dict[str, object]:
        blob = self.bucket.get_blob(self._blob_path(path))
        if blob is None:
            return {}
        updated = blob.updated
        if not isinstance(updated, datetime):
            raise TypeError("GCS blob has no updated datetime")
        checksum = base64.b64decode(blob.md5_hash).hex() if isinstance(blob.md5_hash, str) else None
        return {"last_modified": updated.timestamp(), "checksum": checksum}

    async def stat_file(
        self,
        path: str,
        info: MediaPipeline.SpiderInfo,
    ) -> dict[str, object]:
        del info
        return await asyncio.to_thread(self._stat_file, path)

    def _persist_file(
        self,
        path: str,
        body: bytes,
        meta: Mapping[str, object],
        headers: Mapping[str, str],
    ) -> None:
        blob = self.bucket.blob(self._blob_path(path))
        blob.cache_control = self.CACHE_CONTROL
        blob.metadata = {name: str(value) for name, value in meta.items()}
        blob.upload_from_string(
            body,
            content_type=headers.get("Content-Type", "application/octet-stream"),
            predefined_acl=self.policy,
        )

    async def persist_file(
        self,
        path: str,
        buf: BytesIO,
        info: MediaPipeline.SpiderInfo,
        meta: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        del info
        await asyncio.to_thread(
            self._persist_file,
            path,
            buf.getvalue(),
            meta or {},
            headers or {},
        )


def _ftp_makedirs(ftp: FTP, directory: str) -> None:
    current = "/" if posixpath.isabs(directory) else ""
    for component in directory.split("/"):
        if not component:
            continue
        current = posixpath.join(current, component)
        try:
            ftp.mkd(current)
        except error_perm as error:
            if not str(error).startswith("550"):
                raise


class FTPFilesStore:
    def __init__(
        self,
        uri: str,
        *,
        username: str,
        password: str,
        use_active_mode: bool = False,
    ) -> None:
        parsed = urlparse(uri)
        if parsed.scheme != "ftp" or not parsed.hostname:
            raise ValueError(f"Incorrect FTP media storage URI: {uri}")
        self.host = parsed.hostname
        self.port = parsed.port or 21
        self.username = unquote(parsed.username) if parsed.username else username
        self.password = unquote(parsed.password) if parsed.password else password
        self.basedir = parsed.path.rstrip("/")
        self.use_active_mode = use_active_mode

    @classmethod
    def from_crawler(
        cls,
        crawler: Crawler,
        uri: str,
        *,
        media_name: str,
    ) -> FTPFilesStore:
        del media_name
        return cls(
            uri,
            username=str(crawler.settings.get("FTP_USER", "anonymous")),
            password=str(crawler.settings.get("FTP_PASSWORD", "guest")),
            use_active_mode=crawler.settings.getbool("FEED_STORAGE_FTP_ACTIVE", False),
        )

    def _path(self, path: str) -> str:
        return posixpath.join(self.basedir, path)

    def _connect(self) -> FTP:
        ftp = FTP()
        ftp.connect(self.host, self.port)
        ftp.login(self.username, self.password)
        if self.use_active_mode:
            ftp.set_pasv(False)
        return ftp

    def _persist_file(self, path: str, body: bytes) -> None:
        with self._connect() as ftp:
            remote_path = self._path(path)
            _ftp_makedirs(ftp, posixpath.dirname(remote_path))
            ftp.storbinary(f"STOR {remote_path}", BytesIO(body))

    async def persist_file(
        self,
        path: str,
        buf: BytesIO,
        info: MediaPipeline.SpiderInfo,
        meta: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        del info, meta, headers
        await asyncio.to_thread(self._persist_file, path, buf.getvalue())

    def _stat_file(self, path: str) -> dict[str, object]:
        try:
            with self._connect() as ftp:
                remote_path = self._path(path)
                modified_response = ftp.voidcmd(f"MDTM {remote_path}")
                modified = datetime.strptime(
                    modified_response.removeprefix("213 ").strip()[:14],
                    "%Y%m%d%H%M%S",
                ).replace(tzinfo=timezone.utc)
                digest = hashlib.md5()  # noqa: S324
                ftp.retrbinary(f"RETR {remote_path}", digest.update)
        except error_perm as error:
            if str(error).startswith("550"):
                return {}
            raise
        return {"last_modified": modified.timestamp(), "checksum": digest.hexdigest()}

    async def stat_file(
        self,
        path: str,
        info: MediaPipeline.SpiderInfo,
    ) -> dict[str, object]:
        del info
        return await asyncio.to_thread(self._stat_file, path)


class FilesPipeline(MediaPipeline):
    MEDIA_NAME = "file"
    EXPIRES = 90
    STORE_SCHEMES: ClassVar[dict[str, type[FilesStoreProtocol]]] = {
        "": FSFilesStore,
        "file": FSFilesStore,
        "ftp": FTPFilesStore,
        "gs": GCSFilesStore,
        "s3": S3FilesStore,
    }
    DEFAULT_FILES_URLS_FIELD = "file_urls"
    DEFAULT_FILES_RESULT_FIELD = "files"

    def __init__(
        self,
        store_uri: str | PathLike[str],
        download_func: object = None,
        *,
        crawler: Crawler,
    ) -> None:
        if not store_uri:
            setting_name = (
                "IMAGES_STORE" if self.__class__.__name__ == "ImagesPipeline" else "FILES_STORE"
            )
            raise NotConfigured(
                f"{setting_name} setting must be set to a valid path (not empty) "
                f"to enable {self.__class__.__name__}."
            )
        self.store = self._get_store(str(store_uri), crawler)
        super().__init__(download_func, crawler=crawler)
        resolve = functools.partial(self._key_for_pipe, base_class_name="FilesPipeline")
        self.expires = crawler.settings.getint(resolve("FILES_EXPIRES"), self.EXPIRES)
        urls_field = getattr(self, "FILES_URLS_FIELD", self.DEFAULT_FILES_URLS_FIELD)
        result_field = getattr(self, "FILES_RESULT_FIELD", self.DEFAULT_FILES_RESULT_FIELD)
        self.files_urls_field = str(crawler.settings.get(resolve("FILES_URLS_FIELD"), urls_field))
        self.files_result_field = str(
            crawler.settings.get(resolve("FILES_RESULT_FIELD"), result_field)
        )

    @classmethod
    def from_crawler(cls, crawler: Crawler) -> FilesPipeline:
        return cls(crawler.settings.get("FILES_STORE"), crawler=crawler)

    def _get_store(self, uri: str, crawler: Crawler) -> FilesStoreProtocol:
        scheme = "file" if Path(uri).is_absolute() else urlparse(uri).scheme
        try:
            store_class = self.STORE_SCHEMES[scheme]
        except KeyError:
            supported = ", ".join(repr(value or "local path") for value in self.STORE_SCHEMES)
            raise NotConfigured(
                f"unsupported media store scheme {scheme!r}; supported schemes: {supported}"
            ) from None
        if issubclass(store_class, (S3FilesStore, GCSFilesStore, FTPFilesStore)):
            return store_class.from_crawler(
                crawler,
                uri,
                media_name=self.MEDIA_NAME,
            )
        return store_class(uri)

    async def media_to_download(
        self,
        request: Request,
        info: MediaPipeline.SpiderInfo,
        *,
        item: Any = None,
    ) -> FileInfo | None:
        path = self.file_path(request, info=info, item=item)
        try:
            result = await maybe_await(self.store.stat_file(path, info))
        except Exception:
            logger.exception(
                "%s.store.stat_file",
                self.__class__.__name__,
                extra={"spider": info.spider},
            )
            return None
        if not result or not isinstance(result, Mapping):
            return None
        modified = result.get("last_modified")
        if not isinstance(modified, (int, float)):
            return None
        if (time.time() - modified) / 86400 > self.expires:
            return None
        self.inc_stats("uptodate")
        checksum = result.get("checksum")
        return {
            "url": request.url,
            "path": path,
            "checksum": str(checksum) if checksum is not None else None,
            "status": "uptodate",
        }

    async def media_downloaded(
        self,
        response: Response,
        request: Request,
        info: MediaPipeline.SpiderInfo,
        *,
        item: Any = None,
    ) -> FileInfo:
        if response.status != 200:
            raise FileException("download-error")
        if not response.body:
            raise FileException("empty-content")
        status = "cached" if "cached" in response.flags else "downloaded"
        self.inc_stats(status)
        try:
            path = self.file_path(request, response=response, info=info, item=item)
            checksum = await maybe_await(self.file_downloaded(response, request, info, item=item))
        except FileException:
            raise
        except Exception as error:
            raise FileException(str(error)) from error
        return {
            "url": request.url,
            "path": path,
            "checksum": checksum,
            "status": status,
        }

    def media_failed(
        self,
        error: Exception,
        request: Request,
        info: MediaPipeline.SpiderInfo,
    ) -> object:
        logger.warning(
            "Error downloading %s from %s: %s",
            self.MEDIA_NAME,
            request.url,
            error,
            extra={"spider": info.spider},
        )
        raise FileException from error

    def inc_stats(self, status: str) -> None:
        self.crawler.stats.inc_value("file_count")
        self.crawler.stats.inc_value(f"file_status_count/{status}")

    def get_media_requests(
        self,
        item: Any,
        info: MediaPipeline.SpiderInfo,
    ) -> list[Request]:
        del info
        urls = ItemAdapter(item).get(self.files_urls_field, [])
        if not isinstance(urls, list):
            raise TypeError(
                f"{self.files_urls_field} must be a list of URLs, got {type(urls).__name__}."
            )
        return [Request(url) for url in urls]

    async def file_downloaded(
        self,
        response: Response,
        request: Request,
        info: MediaPipeline.SpiderInfo,
        *,
        item: Any = None,
    ) -> str:
        path = self.file_path(request, response=response, info=info, item=item)
        content = BytesIO(response.body)
        persisted = await maybe_await(self.store.persist_file(path, content, info))
        if isinstance(persisted, str):
            return persisted
        return hashlib.md5(response.body).hexdigest()  # noqa: S324

    def item_completed(
        self,
        results: list[tuple[bool, FileInfo | BaseException]],
        item: Any,
        info: MediaPipeline.SpiderInfo,
    ) -> Any | Awaitable[Any]:
        super().item_completed(results, item, info)
        with suppress(KeyError):
            ItemAdapter(item)[self.files_result_field] = [
                value for success, value in results if success
            ]
        return item

    def file_path(
        self,
        request: Request,
        response: Response | None = None,
        info: MediaPipeline.SpiderInfo | None = None,
        *,
        item: Any = None,
    ) -> str:
        del response, info, item
        media_guid = hashlib.sha1(request.url.encode()).hexdigest()  # noqa: S324
        parsed = urlparse(request.url)
        media_extension = Path(parsed.path).suffix
        if media_extension not in mimetypes.types_map:
            media_extension = Path(request.url).suffix
        if media_extension not in mimetypes.types_map:
            media_type = mimetypes.guess_type(request.url)[0]
            media_extension = mimetypes.guess_extension(media_type) if media_type else ""
        return f"full/{media_guid}{media_extension or ''}"
