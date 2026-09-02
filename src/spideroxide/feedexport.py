from __future__ import annotations

import asyncio
import csv
import json
import logging
import posixpath
import re
import sys
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable, Mapping
from contextlib import closing
from dataclasses import asdict, dataclass, is_dataclass
from datetime import date, datetime, time, timezone
from decimal import Decimal
from ftplib import FTP, error_perm
from io import TextIOWrapper
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, BinaryIO
from urllib.parse import unquote, urlsplit
from xml.sax.saxutils import XMLGenerator
from xml.sax.xmlreader import AttributesImpl

from itemadapter import ItemAdapter

from . import signals
from .components import load_object
from .exceptions import NotConfigured
from .feedpostprocessing import PostProcessingManager
from .http import Request, Response
from .utils import maybe_await

logger = logging.getLogger(__name__)
_URI_PARAMETER = re.compile(r"%\([^)]+\)[#0\- +]*\d*(?:\.\d+)?[a-zA-Z]")


def _item_mapping(item: object) -> dict[str, object]:
    if isinstance(item, Mapping):
        return dict(item)
    if is_dataclass(item) and not isinstance(item, type):
        return asdict(item)
    model_dump = getattr(item, "model_dump", None)
    if callable(model_dump):
        value = model_dump()
        if isinstance(value, Mapping):
            return dict(value)
    attrs_fields = getattr(type(item), "__attrs_attrs__", None)
    if attrs_fields is not None:
        return {field.name: getattr(item, field.name) for field in attrs_fields}
    items = getattr(item, "items", None)
    if callable(items):
        return dict(items())
    raise TypeError(f"unsupported feed item type: {type(item).__name__}")


def _path_template_uri(path: Path) -> str:
    templates: list[str] = []

    def mask(match: re.Match[str]) -> str:
        templates.append(match.group())
        return f"__SPIDEROXIDE_URI_PARAMETER_{len(templates) - 1}__"

    masked = _URI_PARAMETER.sub(mask, str(path.absolute()))
    uri = Path(masked).as_uri()
    for index, template in enumerate(templates):
        uri = uri.replace(f"__SPIDEROXIDE_URI_PARAMETER_{index}__", template)
    return uri


def _format_uri_template(template: str, params: Mapping[str, object]) -> str:
    return _URI_PARAMETER.sub(lambda match: match.group() % params, template)


class SpiderOxideJSONEncoder(json.JSONEncoder):
    def default(self, value: object) -> object:
        if isinstance(value, bytes):
            return value.decode("utf-8")
        if isinstance(value, set):
            return list(value)
        if isinstance(value, datetime):
            return value.strftime("%Y-%m-%d %H:%M:%S")
        if isinstance(value, date):
            return value.strftime("%Y-%m-%d")
        if isinstance(value, time):
            return value.strftime("%H:%M:%S")
        if isinstance(value, Decimal):
            return str(value)
        if isinstance(value, Request):
            return f"<{type(value).__name__} {value.method} {value.url}>"
        if isinstance(value, Response):
            return f"<{type(value).__name__} {value.status} {value.url}>"
        try:
            return _item_mapping(value)
        except TypeError:
            return super().default(value)


class BaseItemExporter:
    def __init__(
        self,
        file: BinaryIO,
        *,
        encoding: str | None = None,
        fields_to_export: Mapping[str, str] | Iterable[str] | None = None,
        export_empty_fields: bool = False,
        indent: int | None = None,
        **kwargs: object,
    ) -> None:
        self.file = file
        self.encoding = encoding
        self.fields_to_export = fields_to_export
        self.export_empty_fields = export_empty_fields
        self.indent = indent
        self._kwargs = kwargs

    def start_exporting(self) -> None:
        pass

    def finish_exporting(self) -> None:
        pass

    def serialize_field(
        self,
        field: Mapping[str, object],
        name: str,
        value: object,
    ) -> object:
        serializer: Callable[[object], object] = field.get("serializer", lambda item: item)  # type: ignore[assignment]
        return serializer(value)

    def _serialized_fields(
        self,
        item: object,
        *,
        default_value: object = None,
        include_empty: bool | None = None,
    ) -> Iterable[tuple[str, object]]:
        include_empty = self.export_empty_fields if include_empty is None else include_empty
        adapter = ItemAdapter(item) if ItemAdapter.is_item(item) else None
        values = dict(adapter.items()) if adapter is not None else _item_mapping(item)
        fields = self.fields_to_export
        if fields is None:
            field_iter: Iterable[str | tuple[str, str]] = (
                adapter.field_names() if adapter is not None and include_empty else values
            )
        elif isinstance(fields, Mapping):
            field_iter = (
                fields.items()
                if include_empty
                else ((name, output) for name, output in fields.items() if name in values)
            )
        elif include_empty:
            field_iter = fields
        else:
            field_iter = (name for name in fields if name in values)

        for field in field_iter:
            input_name, output_name = (field, field) if isinstance(field, str) else field
            if input_name in values:
                metadata = adapter.get_field_meta(input_name) if adapter is not None else {}
                yield (
                    output_name,
                    self.serialize_field(
                        metadata,
                        output_name,
                        values[input_name],
                    ),
                )
            elif include_empty:
                yield output_name, default_value

    def export_item(self, item: object) -> None:
        raise NotImplementedError


class JsonItemExporter(BaseItemExporter):
    def __init__(self, file: BinaryIO, **kwargs: object) -> None:
        super().__init__(file, **kwargs)
        encoder_options = dict(self._kwargs)
        encoder_options.setdefault(
            "indent", self.indent if self.indent and self.indent > 0 else None
        )
        encoder_options.setdefault("ensure_ascii", not self.encoding)
        self.encoder = SpiderOxideJSONEncoder(**encoder_options)
        self.first_item = True

    def _newline(self) -> None:
        if self.indent is not None:
            self.file.write(b"\n")

    def start_exporting(self) -> None:
        self.file.write(b"[")
        self._newline()

    def export_item(self, item: object) -> None:
        encoded = self.encoder.encode(dict(self._serialized_fields(item)))
        data = encoded.encode(self.encoding or "utf-8")
        if self.first_item:
            self.first_item = False
        else:
            self.file.write(b",")
            self._newline()
        self.file.write(data)

    def finish_exporting(self) -> None:
        self._newline()
        self.file.write(b"]")


class JsonLinesItemExporter(BaseItemExporter):
    def __init__(self, file: BinaryIO, **kwargs: object) -> None:
        super().__init__(file, **kwargs)
        encoder_options = dict(self._kwargs)
        encoder_options.setdefault("ensure_ascii", not self.encoding)
        self.encoder = SpiderOxideJSONEncoder(**encoder_options)

    def export_item(self, item: object) -> None:
        encoded = self.encoder.encode(dict(self._serialized_fields(item))) + "\n"
        self.file.write(encoded.encode(self.encoding or "utf-8"))


class CsvItemExporter(BaseItemExporter):
    def __init__(
        self,
        file: BinaryIO,
        *,
        include_headers_line: bool = True,
        join_multivalued: str = ",",
        errors: str | None = None,
        **kwargs: object,
    ) -> None:
        super().__init__(file, **kwargs)
        self.encoding = self.encoding or "utf-8"
        self.include_headers_line = include_headers_line
        self.join_multivalued = join_multivalued
        self.errors = errors or "strict"
        self.stream = TextIOWrapper(
            file,
            encoding=self.encoding,
            errors=errors,
            newline="",
            write_through=True,
        )
        self.writer = csv.writer(self.stream, **self._kwargs)
        self.headers_written = False

    def _cell(self, value: object) -> object:
        if isinstance(value, (list, tuple)):
            try:
                return self.join_multivalued.join(value)
            except TypeError:
                pass
        return value

    def _row_value(self, value: object) -> object:
        if isinstance(value, bytes):
            return value.decode(self.encoding, errors=self.errors)
        return value

    def serialize_field(
        self,
        field: Mapping[str, object],
        name: str,
        value: object,
    ) -> object:
        serializer: Callable[[object], object] = field.get("serializer", self._cell)  # type: ignore[assignment]
        return serializer(value)

    def export_item(self, item: object) -> None:
        if not self.headers_written:
            self.headers_written = True
            if self.fields_to_export is None:
                self.fields_to_export = tuple(
                    ItemAdapter(item).field_names()
                    if ItemAdapter.is_item(item)
                    else _item_mapping(item)
                )
            if self.include_headers_line:
                headers = (
                    self.fields_to_export.values()
                    if isinstance(self.fields_to_export, Mapping)
                    else self.fields_to_export
                )
                self.writer.writerow(headers)
        fields = self._serialized_fields(item, default_value="", include_empty=True)
        self.writer.writerow(self._row_value(value) for _, value in fields)

    def finish_exporting(self) -> None:
        self.stream.detach()


class XmlItemExporter(BaseItemExporter):
    def __init__(
        self,
        file: BinaryIO,
        *,
        item_element: str = "item",
        root_element: str = "items",
        **kwargs: object,
    ) -> None:
        super().__init__(file, **kwargs)
        if self._kwargs:
            raise TypeError(f"unexpected XML exporter options: {', '.join(self._kwargs)}")
        self.encoding = self.encoding or "utf-8"
        self.item_element = item_element
        self.root_element = root_element
        self.stream = TextIOWrapper(
            file,
            encoding=self.encoding,
            errors="xmlcharrefreplace",
            newline="\n",
            write_through=True,
        )
        self.generator = XMLGenerator(self.stream, encoding=self.encoding)

    def _newline(self, *, new_item: bool = False) -> None:
        if self.indent is not None and (self.indent > 0 or new_item):
            self.generator.characters("\n")

    def _indent(self, depth: int) -> None:
        if self.indent:
            self.generator.characters(" " * self.indent * depth)

    def start_exporting(self) -> None:
        self.generator.startDocument()
        self.generator.startElement(self.root_element, AttributesImpl({}))
        self._newline(new_item=True)

    def _export_field(self, name: str, value: object, depth: int) -> None:
        self._indent(depth)
        self.generator.startElement(name, AttributesImpl({}))
        if isinstance(value, Mapping):
            self._newline()
            for child_name, child_value in value.items():
                self._export_field(str(child_name), child_value, depth + 1)
            self._indent(depth)
        elif isinstance(value, Iterable) and not isinstance(value, (str, bytes)):
            self._newline()
            for child_value in value:
                self._export_field("value", child_value, depth + 1)
            self._indent(depth)
        else:
            self.generator.characters(value if isinstance(value, str) else str(value))
        self.generator.endElement(name)
        self._newline()

    def export_item(self, item: object) -> None:
        self._indent(1)
        self.generator.startElement(self.item_element, AttributesImpl({}))
        self._newline()
        for name, value in self._serialized_fields(item, default_value=""):
            self._export_field(name, value, 2)
        self._indent(1)
        self.generator.endElement(self.item_element)
        self._newline(new_item=True)

    def finish_exporting(self) -> None:
        self.generator.endElement(self.root_element)
        self.generator.endDocument()
        self.stream.detach()


class FileFeedStorage:
    def __init__(self, uri: str, *, feed_options: Mapping[str, object] | None = None) -> None:
        parsed = urlsplit(uri)
        if parsed.scheme == "file":
            host = "" if parsed.netloc in {"", "localhost"} else f"//{parsed.netloc}"
            self.path = Path(host + unquote(parsed.path))
        else:
            self.path = Path(uri)
        self.overwrite = bool((feed_options or {}).get("overwrite", False))

    def open(self, spider: object) -> BinaryIO:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        return self.path.open("wb" if self.overwrite else "ab")

    def store(self, file: BinaryIO) -> None:
        file.close()


class StdoutFeedStorage:
    def __init__(self, uri: str, *, feed_options: Mapping[str, object] | None = None) -> None:
        self.uri = uri
        if feed_options and feed_options.get("overwrite", False) is True:
            logger.warning(
                "Standard output storage does not support overwriting. "
                "Remove the overwrite option or set it to False."
            )

    def open(self, spider: object) -> BinaryIO:
        return sys.stdout.buffer

    def store(self, file: BinaryIO) -> None:
        file.flush()


class BlockingFeedStorage(ABC):
    def open(self, spider: object) -> BinaryIO:
        crawler = getattr(spider, "crawler", None)
        settings = getattr(crawler, "settings", None)
        temporary_directory = settings.get("FEED_TEMPDIR") if settings is not None else None
        if temporary_directory and not Path(temporary_directory).is_dir():
            raise OSError(f"Not a Directory: {temporary_directory}")
        return NamedTemporaryFile(prefix="feed-", dir=temporary_directory)

    async def store(self, file: BinaryIO) -> None:
        await asyncio.to_thread(self._store_in_thread, file)

    @abstractmethod
    def _store_in_thread(self, file: BinaryIO) -> None:
        raise NotImplementedError


class S3FeedStorage(BlockingFeedStorage):
    def __init__(
        self,
        uri: str,
        access_key: str | None = None,
        secret_key: str | None = None,
        acl: str | None = None,
        endpoint_url: str | None = None,
        *,
        feed_options: Mapping[str, object] | None = None,
        session_token: str | None = None,
        region_name: str | None = None,
    ) -> None:
        try:
            import boto3.session
        except ImportError:
            raise NotConfigured("missing boto3 library") from None
        parsed = urlsplit(uri)
        if not parsed.hostname:
            raise ValueError(f"Got a storage URI without a hostname: {uri}")
        self.bucketname = parsed.hostname
        self.access_key = parsed.username or access_key
        self.secret_key = parsed.password or secret_key
        self.session_token = session_token
        self.keyname = parsed.path[1:]
        self.acl = acl
        self.endpoint_url = endpoint_url
        self.region_name = region_name
        self.s3_client = boto3.session.Session().client(
            "s3",
            aws_access_key_id=self.access_key,
            aws_secret_access_key=self.secret_key,
            aws_session_token=self.session_token,
            endpoint_url=self.endpoint_url,
            region_name=self.region_name,
        )
        if feed_options and feed_options.get("overwrite", True) is False:
            logger.warning(
                "S3 storage does not support appending; the remote object will be replaced."
            )

    @classmethod
    def from_crawler(
        cls,
        crawler: object,
        uri: str,
        *,
        feed_options: Mapping[str, object] | None = None,
    ) -> S3FeedStorage:
        settings = crawler.settings  # type: ignore[attr-defined]
        return cls(
            uri,
            access_key=settings.get("AWS_ACCESS_KEY_ID"),
            secret_key=settings.get("AWS_SECRET_ACCESS_KEY"),
            session_token=settings.get("AWS_SESSION_TOKEN"),
            acl=settings.get("FEED_STORAGE_S3_ACL") or None,
            endpoint_url=settings.get("AWS_ENDPOINT_URL") or None,
            region_name=settings.get("AWS_REGION_NAME") or None,
            feed_options=feed_options,
        )

    def _store_in_thread(self, file: BinaryIO) -> None:
        file.seek(0)
        try:
            arguments: dict[str, object] = {
                "Bucket": self.bucketname,
                "Key": self.keyname,
                "Fileobj": file,
            }
            if self.acl:
                arguments["ExtraArgs"] = {"ACL": self.acl}
            self.s3_client.upload_fileobj(**arguments)
        finally:
            file.close()


class GCSFeedStorage(BlockingFeedStorage):
    def __init__(
        self,
        uri: str,
        project_id: str | None,
        acl: str | None,
        *,
        feed_options: Mapping[str, object] | None = None,
    ) -> None:
        parsed = urlsplit(uri)
        if not parsed.hostname:
            raise ValueError(f"Got a storage URI without a hostname: {uri}")
        self.project_id = project_id
        self.acl = acl
        self.bucket_name = parsed.hostname
        self.blob_name = parsed.path[1:]
        if feed_options and feed_options.get("overwrite", True) is False:
            logger.warning(
                "GCS storage does not support appending; the remote object will be replaced."
            )

    @classmethod
    def from_crawler(
        cls,
        crawler: object,
        uri: str,
        *,
        feed_options: Mapping[str, object] | None = None,
    ) -> GCSFeedStorage:
        settings = crawler.settings  # type: ignore[attr-defined]
        return cls(
            uri,
            settings.get("GCS_PROJECT_ID"),
            settings.get("FEED_STORAGE_GCS_ACL") or None,
            feed_options=feed_options,
        )

    def _store_in_thread(self, file: BinaryIO) -> None:
        file.seek(0)
        try:
            from google.cloud.storage import Client

            client = Client(project=self.project_id)
            bucket = client.get_bucket(self.bucket_name)
            blob = bucket.blob(self.blob_name)
            blob.upload_from_file(file, predefined_acl=self.acl)
        finally:
            file.close()


def _ftp_makedirs_cwd(ftp: FTP, path: str, first_call: bool = True) -> None:
    try:
        ftp.cwd(path)
    except error_perm:
        _ftp_makedirs_cwd(ftp, posixpath.dirname(path), False)
        try:
            ftp.mkd(path)
        except error_perm:
            ftp.cwd(path)
        else:
            if first_call:
                ftp.cwd(path)


def _ftp_store_file(
    *,
    path: str,
    file: BinaryIO,
    host: str,
    port: int,
    username: str,
    password: str,
    use_active_mode: bool,
    overwrite: bool,
) -> None:
    with FTP() as ftp, closing(file):
        ftp.connect(host, port)
        ftp.login(username, password)
        if use_active_mode:
            ftp.set_pasv(False)
        file.seek(0)
        directory, filename = posixpath.split(path)
        _ftp_makedirs_cwd(ftp, directory)
        command = "STOR" if overwrite else "APPE"
        ftp.storbinary(f"{command} {filename}", file)


class FTPFeedStorage(BlockingFeedStorage):
    def __init__(
        self,
        uri: str,
        use_active_mode: bool = False,
        *,
        feed_options: Mapping[str, object] | None = None,
    ) -> None:
        parsed = urlsplit(uri)
        if not parsed.hostname:
            raise ValueError(f"Got a storage URI without a hostname: {uri}")
        self.host = parsed.hostname
        self.port = parsed.port or 21
        self.username = parsed.username or ""
        self.password = unquote(parsed.password or "")
        self.path = parsed.path
        self.use_active_mode = use_active_mode
        self.overwrite = not feed_options or bool(feed_options.get("overwrite", True))

    @classmethod
    def from_crawler(
        cls,
        crawler: object,
        uri: str,
        *,
        feed_options: Mapping[str, object] | None = None,
    ) -> FTPFeedStorage:
        return cls(
            uri,
            use_active_mode=crawler.settings.getbool("FEED_STORAGE_FTP_ACTIVE"),  # type: ignore[attr-defined]
            feed_options=feed_options,
        )

    def _store_in_thread(self, file: BinaryIO) -> None:
        _ftp_store_file(
            path=self.path,
            file=file,
            host=self.host,
            port=self.port,
            username=self.username,
            password=self.password,
            use_active_mode=self.use_active_mode,
            overwrite=self.overwrite,
        )


class ItemFilter:
    def __init__(self, feed_options: Mapping[str, object] | None = None) -> None:
        references = (feed_options or {}).get("item_classes") or ()
        if isinstance(references, (str, type)):
            references = (references,)
        self.item_classes = tuple(
            load_object(reference) if isinstance(reference, str) else reference
            for reference in references  # type: ignore[union-attr]
        )

    def accepts(self, item: object) -> bool:
        return not self.item_classes or isinstance(item, self.item_classes)


@dataclass(slots=True)
class FeedSlot:
    storage: object
    uri: str
    format: str
    store_empty: bool
    batch_id: int
    uri_template: str
    filter: object
    feed_options: dict[str, object]
    spider: object
    crawler: object
    exporters: dict[str, object]
    file: BinaryIO | None = None
    exporter: BaseItemExporter | None = None
    itemcount: int = 0
    exporting: bool = False
    failed: bool = False
    closed: bool = False

    def start_exporting(self) -> None:
        if self.file is None:
            self.file = self.storage.open(self.spider)  # type: ignore[attr-defined]
            if "postprocessing" in self.feed_options:
                plugins = self.feed_options["postprocessing"]
                if not isinstance(plugins, list):
                    raise TypeError("feed postprocessing must be a list")
                self.file = PostProcessingManager(plugins, self.file, self.feed_options)
            exporter_type = self.exporters[self.format]
            options = self.feed_options
            self.exporter = _build_from_crawler(
                exporter_type,
                self.crawler,
                self.file,
                fields_to_export=options["fields"],
                encoding=options["encoding"],
                indent=options["indent"],
                **options["item_export_kwargs"],  # type: ignore[arg-type]
            )
        if not self.exporting:
            self.exporter.start_exporting()
            self.exporting = True

    def finish_exporting(self) -> None:
        if self.exporting and self.exporter is not None:
            self.exporter.finish_exporting()
            self.exporting = False

    def storage_file(self) -> BinaryIO:
        if self.file is None:
            raise RuntimeError("feed slot has no open file")
        if isinstance(self.file, PostProcessingManager):
            self.file.close()
            return self.file.file
        return self.file


def _build_from_crawler(
    reference: object,
    crawler: object,
    *args: object,
    **kwargs: object,
) -> Any:
    component = load_object(reference) if isinstance(reference, str) else reference
    if not callable(component):
        raise TypeError(f"feed component is not callable: {component!r}")
    from_crawler = getattr(component, "from_crawler", None)
    return from_crawler(crawler, *args, **kwargs) if from_crawler else component(*args, **kwargs)


def _load_component_mapping(base: object, custom: object, setting_name: str) -> dict[str, object]:
    if not isinstance(base, Mapping) or not isinstance(custom, Mapping):
        raise TypeError(f"{setting_name} settings must be mappings")
    merged = dict(base)
    merged.update(custom)
    return {
        str(name): load_object(reference) if isinstance(reference, str) else reference
        for name, reference in merged.items()
        if reference is not None
    }


class FeedExporter:
    def __init__(self, crawler: object) -> None:
        self.crawler = crawler
        self.settings = crawler.settings  # type: ignore[attr-defined]
        self.stats = crawler.stats  # type: ignore[attr-defined]
        self.feeds = self._load_feeds()
        if not self.feeds:
            raise NotConfigured
        self.storages = _load_component_mapping(
            self.settings.get("FEED_STORAGES_BASE", {}),
            self.settings.get("FEED_STORAGES", {}),
            "FEED_STORAGES",
        )
        self.exporters = _load_component_mapping(
            self.settings.get("FEED_EXPORTERS_BASE", {}),
            self.settings.get("FEED_EXPORTERS", {}),
            "FEED_EXPORTERS",
        )
        self.filters = {uri: self._build_filter(options) for uri, options in self.feeds.items()}
        self.slots: list[FeedSlot] = []
        self._pending_closes: set[asyncio.Task[None]] = set()
        self._pending_close_errors: list[BaseException] = []
        self._storage_concurrency = self.settings.getint("FEED_STORAGE_CONCURRENCY", 4)
        if self._storage_concurrency < 1:
            raise ValueError("FEED_STORAGE_CONCURRENCY must be at least 1")
        self._slot_lock = asyncio.Lock()
        self._validate_feeds()

    @classmethod
    def from_crawler(cls, crawler: object) -> FeedExporter:
        exporter = cls(crawler)
        crawler.signals.connect(exporter.open_spider, signals.spider_opened)  # type: ignore[attr-defined]
        crawler.signals.connect(exporter.item_scraped, signals.item_scraped)  # type: ignore[attr-defined]
        crawler.signals.connect(exporter.close_spider, signals.spider_closed)  # type: ignore[attr-defined]
        return exporter

    def _load_feeds(self) -> dict[str, dict[str, object]]:
        configured = self.settings.get("FEEDS", {})
        if not isinstance(configured, Mapping):
            raise TypeError("FEEDS must be a mapping")
        feeds: dict[str, dict[str, object]] = {}
        legacy_uri = self.settings.get("FEED_URI")
        if legacy_uri:
            feeds[str(legacy_uri)] = self._complete_options(
                {"format": self.settings.get("FEED_FORMAT", "jsonlines")}
            )
        for configured_uri, options in configured.items():
            if not isinstance(options, Mapping):
                raise TypeError(f"feed options for {configured_uri!r} must be a mapping")
            uri = (
                _path_template_uri(configured_uri)
                if isinstance(configured_uri, Path)
                else str(configured_uri)
            )
            feeds[uri] = self._complete_options(options)
        return feeds

    def _complete_options(self, configured: Mapping[str, object]) -> dict[str, object]:
        options = dict(configured)
        options.setdefault("batch_item_count", self.settings.getint("FEED_EXPORT_BATCH_ITEM_COUNT"))
        options.setdefault("encoding", self.settings.get("FEED_EXPORT_ENCODING"))
        options.setdefault("fields", self.settings.get("FEED_EXPORT_FIELDS"))
        options.setdefault("store_empty", self.settings.getbool("FEED_STORE_EMPTY"))
        options.setdefault("uri_params", self.settings.get("FEED_URI_PARAMS"))
        options.setdefault("item_export_kwargs", {})
        options.setdefault("indent", self.settings.get("FEED_EXPORT_INDENT"))
        return options

    def _build_filter(self, options: dict[str, object]) -> object:
        reference = options.get("item_filter", ItemFilter)
        return _build_from_crawler(reference, self.crawler, options)

    def _validate_feeds(self) -> None:
        for uri, options in self.feeds.items():
            format_name = options.get("format")
            if not isinstance(format_name, str) or format_name not in self.exporters:
                logger.error("Unknown feed format for %r: %r", uri, format_name)
                raise NotConfigured(f"unknown feed format for {uri!r}: {format_name!r}")
            scheme = urlsplit(uri).scheme
            if scheme not in self.storages:
                logger.error("Unsupported feed URI scheme for %r: %r", uri, scheme)
                raise NotConfigured(f"unsupported feed URI scheme for {uri!r}: {scheme!r}")
            try:
                self._storage(uri, options)
            except NotConfigured as error:
                logger.error("Disabled feed storage scheme %r: %s", scheme, error)
                raise
            batch_count = options["batch_item_count"]
            if not isinstance(batch_count, int) or batch_count < 0:
                raise ValueError("feed batch_item_count must be a non-negative integer")
            if batch_count and "%(batch_id)" not in uri and "%(batch_time)" not in uri:
                logger.error("batch feed URI templates must include %(batch_id)d or %(batch_time)s")
                raise NotConfigured("batch feed URI template is missing a batch placeholder")
            if not isinstance(options["item_export_kwargs"], Mapping):
                raise TypeError("feed item_export_kwargs must be a mapping")
            indent = options["indent"]
            options["indent"] = None if indent is None else int(indent)
            fields = options["fields"]
            if isinstance(fields, str):
                options["fields"] = tuple(
                    field.strip() for field in fields.split(",") if field.strip()
                )
            elif fields is not None and not isinstance(fields, (Mapping, Iterable)):
                raise TypeError("feed fields must be a mapping or iterable")

    def _uri_params(
        self,
        spider: object,
        options: dict[str, object],
        *,
        batch_id: int,
    ) -> dict[str, object]:
        params = {name: getattr(spider, name) for name in dir(spider) if not name.startswith("__")}
        now = datetime.now(tz=timezone.utc)
        params.update(
            {
                "time": now.replace(microsecond=0).isoformat().replace(":", "-"),
                "batch_time": now.isoformat().replace(":", "-"),
                "batch_id": batch_id,
            }
        )
        reference = options["uri_params"]
        if reference:
            callback = load_object(reference) if isinstance(reference, str) else reference
            updated = callback(params, spider)  # type: ignore[operator]
            if updated is not None:
                params = updated
        return params

    def _storage(self, uri: str, options: dict[str, object]) -> object:
        scheme = urlsplit(uri).scheme
        return _build_from_crawler(
            self.storages[scheme],
            self.crawler,
            uri,
            feed_options=options,
        )

    def _new_slot(
        self,
        spider: object,
        uri_template: str,
        options: dict[str, object],
        batch_id: int,
    ) -> FeedSlot:
        uri = _format_uri_template(
            uri_template,
            self._uri_params(spider, options, batch_id=batch_id),
        )
        return FeedSlot(
            storage=self._storage(uri, options),
            uri=uri,
            format=options["format"],  # type: ignore[arg-type]
            store_empty=bool(options["store_empty"]),
            batch_id=batch_id,
            uri_template=uri_template,
            filter=self.filters[uri_template],
            feed_options=options,
            spider=spider,
            crawler=self.crawler,
            exporters=self.exporters,
        )

    def open_spider(self, spider: object) -> None:
        self.slots = [
            self._new_slot(spider, uri, options, 1) for uri, options in self.feeds.items()
        ]

    def _record_failure(self, slot: FeedSlot, message: str) -> None:
        if not slot.failed:
            self.stats.inc_value(f"feedexport/failed_count/{type(slot.storage).__name__}")
        slot.failed = True
        spider_logger = getattr(slot.spider, "logger", logger)
        spider_logger.exception(message)
        if slot.file is not None and slot.file is not sys.stdout.buffer:
            try:
                slot.file.close()
            except Exception:
                spider_logger.error("Error closing failed feed %s", slot.uri, exc_info=True)
            finally:
                if (
                    isinstance(slot.file, PostProcessingManager)
                    and slot.file.file is not sys.stdout.buffer
                ):
                    slot.file.file.close()

    async def item_scraped(self, item: object, spider: object) -> None:
        async with self._slot_lock:
            await self._export_item(item, spider)

    async def _export_item(self, item: object, spider: object) -> None:
        active_slots = []
        for slot in self.slots:
            accepted = False
            try:
                accepted = slot.filter.accepts(item)  # type: ignore[attr-defined]
                if accepted and not slot.failed:
                    slot.start_exporting()
                    slot.exporter.export_item(item)  # type: ignore[union-attr]
                    slot.itemcount += 1
            except Exception:
                self.stats.inc_value("feedexport/item_error_count")
                self._record_failure(slot, f"Error exporting item to {slot.uri}")

            batch_count = slot.feed_options["batch_item_count"]
            if accepted and batch_count and slot.itemcount >= batch_count:
                await self._schedule_close(slot)
                active_slots.append(
                    self._new_slot(
                        spider,
                        slot.uri_template,
                        slot.feed_options,
                        slot.batch_id + 1,
                    )
                )
            else:
                active_slots.append(slot)
        self.slots = active_slots

    async def _schedule_close(self, slot: FeedSlot) -> None:
        if len(self._pending_closes) >= self._storage_concurrency:
            completed, _ = await asyncio.wait(
                tuple(self._pending_closes),
                return_when=asyncio.FIRST_COMPLETED,
            )
            self._pending_closes.difference_update(completed)
        task = asyncio.create_task(self._close_slot(slot))
        self._pending_closes.add(task)

        def completed(finished: asyncio.Task[None]) -> None:
            self._pending_closes.discard(finished)
            if finished.cancelled():
                self._pending_close_errors.append(asyncio.CancelledError())
            elif error := finished.exception():
                self._pending_close_errors.append(error)

        task.add_done_callback(completed)

    async def _close_slot(self, slot: FeedSlot) -> None:
        if slot.closed:
            return
        slot.closed = True
        if slot.failed:
            await self.crawler.signals.send(signals.feed_slot_closed, slot=slot)  # type: ignore[attr-defined]
            return
        if not slot.itemcount and not (slot.store_empty and slot.batch_id == 1):
            return
        try:
            slot.start_exporting()
            slot.finish_exporting()
            await maybe_await(slot.storage.store(slot.storage_file()))  # type: ignore[attr-defined]
        except Exception:
            self._record_failure(slot, f"Error storing {slot.format} feed in {slot.uri}")
        else:
            self.stats.inc_value(f"feedexport/success_count/{type(slot.storage).__name__}")
            spider_logger = getattr(slot.spider, "logger", logger)
            spider_logger.info(
                "Stored %s feed (%d items) in: %s",
                slot.format,
                slot.itemcount,
                slot.uri,
            )
        await self.crawler.signals.send(signals.feed_slot_closed, slot=slot)  # type: ignore[attr-defined]

    async def close_spider(self, spider: object, reason: str) -> None:
        for slot in self.slots:
            await self._schedule_close(slot)
        pending_closes = list(self._pending_closes)
        if pending_closes:
            results = await asyncio.gather(
                *pending_closes,
                return_exceptions=True,
            )
            self._pending_close_errors.extend(
                result for result in results if isinstance(result, BaseException)
            )
        if self._pending_close_errors:
            raise self._pending_close_errors[0]
        await self.crawler.signals.send(signals.feed_exporter_closed)  # type: ignore[attr-defined]
