from __future__ import annotations

import asyncio
import csv
import json
import logging
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime
from io import BytesIO
from pathlib import Path
from xml.etree import ElementTree

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from spideroxide import (
    BaseItemExporter,
    Crawler,
    CsvItemExporter,
    FeedExporter,
    Field,
    Item,
    JsonLinesItemExporter,
    Request,
    Response,
    Spider,
    signals,
)


class FeedDownloader:
    async def fetch(self, request: Request) -> Response:
        return Response(request.url, request=request)

    async def close(self) -> None:
        pass


class FormatItem(Item):
    name = Field()
    value = Field()
    tags = Field()
    created = Field()
    unused = Field()


class SerializedItem(Item):
    name = Field(serializer=str.upper)
    encoded = Field(serializer=lambda value: value.encode("utf-8"))
    missing = Field()


class FormatSpider(Spider):
    name = "format_spider"
    start_urls = ["https://example.test/one", "https://example.test/two"]

    def parse(self, response: Response) -> FormatItem:
        number = 1 if response.url.endswith("/one") else 2
        return FormatItem(
            name=f"café-{number}",
            value=number,
            tags=["red", "blue"],
            created=datetime(2026, 8, number, 12, 30),
            unused=True,
        )


def _verify_item_field_serialization() -> None:
    output = BytesIO()
    exporter = JsonLinesItemExporter(output, export_empty_fields=True)
    exporter.export_item(SerializedItem(name="coffee", encoded="café"))
    assert json.loads(output.getvalue()) == {
        "name": "COFFEE",
        "encoded": "café",
        "missing": None,
    }

    csv_output = BytesIO()
    csv_exporter = CsvItemExporter(csv_output, encoding="utf-8")
    csv_exporter.export_item(SerializedItem(name="coffee", encoded="café"))
    csv_exporter.finish_exporting()
    assert csv_output.getvalue().decode() == "name,encoded,missing\r\nCOFFEE,café,\r\n"


class EmptySpider(Spider):
    name = "empty_spider"
    start_urls = ["https://example.test/empty"]

    def parse(self, response: Response) -> None:
        return None


class BatchSpider(Spider):
    name = "batch_spider"
    start_urls = [f"https://example.test/{index}" for index in range(5)]

    def parse(self, response: Response) -> dict[str, int]:
        return {"value": int(response.url.rsplit("/", 1)[1])}


@dataclass
class Product:
    name: str
    price: int


class FilterSpider(Spider):
    name = "filter_spider"
    start_urls = ["https://example.test/filter"]

    def parse(self, response: Response) -> list[object]:
        return [Product("kept", 10), {"name": "ignored", "price": 20}]


class MinimumPriceFilter:
    @classmethod
    def from_crawler(
        cls,
        crawler: Crawler,
        feed_options: dict[str, object],
    ) -> MinimumPriceFilter:
        instance = cls()
        instance.minimum = int(feed_options["minimum_price"])
        crawler.custom_filter_loaded = True
        return instance

    def accepts(self, item: object) -> bool:
        return isinstance(item, Product) and item.price >= self.minimum


class FeedSignalRecorder:
    @classmethod
    def from_crawler(cls, crawler: Crawler) -> FeedSignalRecorder:
        instance = cls()
        instance.slots = []
        instance.exporter_closed = 0
        crawler.feed_signal_recorder = instance
        crawler.signals.connect(instance.slot_closed, signals.feed_slot_closed)
        crawler.signals.connect(instance.closed, signals.feed_exporter_closed)
        return instance

    def slot_closed(self, slot: object) -> None:
        self.slots.append(
            (
                slot.uri,
                slot.itemcount,
                slot.batch_id,
                slot.failed,
            )
        )

    def closed(self) -> None:
        self.exporter_closed += 1


def uri_params(params: dict[str, object], spider: Spider) -> dict[str, object]:
    params["run"] = "verified"
    return params


class UpperLinesExporter(BaseItemExporter):
    @classmethod
    def from_crawler(
        cls,
        crawler: Crawler,
        file: BytesIO,
        **kwargs: object,
    ) -> UpperLinesExporter:
        crawler.custom_exporter_loaded = True
        return cls(file, **kwargs)

    def export_item(self, item: object) -> None:
        fields = dict(self._serialized_fields(item))
        self.file.write(f"{fields['name'].upper()}\n".encode())


class MemoryStorage:
    @classmethod
    def from_crawler(
        cls,
        crawler: Crawler,
        uri: str,
        *,
        feed_options: dict[str, object],
    ) -> MemoryStorage:
        instance = cls()
        instance.crawler = crawler
        instance.uri = uri
        instance.feed_options = feed_options
        return instance

    def open(self, spider: Spider) -> BytesIO:
        return BytesIO()

    async def store(self, file: BytesIO) -> None:
        await asyncio.sleep(0)
        self.crawler.stored_feed = (self.uri, file.getvalue())
        file.close()


class ConcurrentMemoryStorage(MemoryStorage):
    async def store(self, file: BytesIO) -> None:
        await asyncio.sleep(0.001)
        self.crawler.concurrent_feeds.append(file.getvalue())
        file.close()


class FailingStorage(MemoryStorage):
    async def store(self, file: BytesIO) -> None:
        file.close()
        raise OSError("expected storage failure")


async def _crawl(
    spider: type[Spider],
    engine: str,
    settings: dict[str, object],
) -> Crawler:
    crawler = Crawler(
        spider,
        {
            "CONCURRENT_REQUESTS": 1,
            "ENGINE_BACKEND": engine,
            **settings,
        },
        downloader=FeedDownloader(),
    )
    result = await crawler.crawl()
    assert result.reason == "finished"
    return crawler


async def _verify_formats(engine: str, directory: Path) -> None:
    template = str(directory / "%(name)s-%(run)s")
    feeds = {
        f"{template}.json": {
            "format": "json",
            "fields": {"name": "label", "value": "score", "created": "created"},
            "encoding": "utf-8",
            "indent": 2,
            "overwrite": True,
            "uri_params": uri_params,
        },
        f"{template}.jl": {
            "format": "jsonl",
            "fields": ["value", "name"],
            "encoding": "utf-8",
            "overwrite": True,
            "uri_params": uri_params,
        },
        f"{template}.csv": {
            "format": "csv",
            "fields": {"name": "label", "value": "score", "tags": "tags"},
            "encoding": "utf-8",
            "overwrite": True,
            "uri_params": uri_params,
            "item_export_kwargs": {"delimiter": ";", "join_multivalued": "|"},
        },
        f"{template}.xml": {
            "format": "xml",
            "fields": ["name", "tags"],
            "encoding": "utf-8",
            "indent": 2,
            "overwrite": True,
            "uri_params": uri_params,
            "item_export_kwargs": {
                "root_element": "products",
                "item_element": "product",
            },
        },
    }
    crawler = await _crawl(
        FormatSpider,
        engine,
        {
            "FEEDS": feeds,
            "EXTENSIONS": {FeedSignalRecorder: 10},
        },
    )

    prefix = directory / "format_spider-verified"
    exported_json = json.loads(prefix.with_suffix(".json").read_text())
    assert exported_json == [
        {"label": "café-1", "score": 1, "created": "2026-08-01 12:30:00"},
        {"label": "café-2", "score": 2, "created": "2026-08-02 12:30:00"},
    ]
    exported_lines = [
        json.loads(line) for line in prefix.with_suffix(".jl").read_text().splitlines()
    ]
    assert exported_lines == [
        {"value": 1, "name": "café-1"},
        {"value": 2, "name": "café-2"},
    ]
    with prefix.with_suffix(".csv").open(newline="") as csv_file:
        assert list(csv.reader(csv_file, delimiter=";")) == [
            ["label", "score", "tags"],
            ["café-1", "1", "red|blue"],
            ["café-2", "2", "red|blue"],
        ]
    xml_root = ElementTree.parse(prefix.with_suffix(".xml")).getroot()
    assert xml_root.tag == "products"
    products = xml_root.findall("product")
    assert [product.findtext("name") for product in products] == ["café-1", "café-2"]
    assert [[value.text for value in product.findall("tags/value")] for product in products] == [
        ["red", "blue"],
        ["red", "blue"],
    ]

    recorder = crawler.feed_signal_recorder
    assert len(recorder.slots) == 4
    assert all(
        itemcount == 2 and batch_id == 1 and not failed
        for _, itemcount, batch_id, failed in recorder.slots
    )
    assert recorder.exporter_closed == 1
    assert crawler.stats.get_value("feedexport/success_count/FileFeedStorage") == 4
    assert isinstance(crawler.extensions.get_by_type(FeedExporter), FeedExporter)


async def _verify_batches(engine: str, directory: Path) -> None:
    template = directory / "batch-%(batch_id)02d.jl"
    crawler = await _crawl(
        BatchSpider,
        engine,
        {
            "FEEDS": {
                template: {
                    "format": "jsonlines",
                    "batch_item_count": 2,
                    "overwrite": True,
                }
            },
            "EXTENSIONS": {FeedSignalRecorder: 10},
        },
    )
    batches = []
    for batch_id, expected_count in ((1, 2), (2, 2), (3, 1)):
        path = directory / f"batch-{batch_id:02d}.jl"
        values = [json.loads(line)["value"] for line in path.read_text().splitlines()]
        assert len(values) == expected_count
        batches.extend(values)
    assert batches == [0, 1, 2, 3, 4]
    assert crawler.stats.get_value("feedexport/success_count/FileFeedStorage") == 3
    assert [slot[2] for slot in crawler.feed_signal_recorder.slots] == [1, 2, 3]


async def _verify_concurrent_batches(engine: str) -> None:
    crawler = Crawler(
        BatchSpider,
        {
            "CONCURRENT_REQUESTS": 4,
            "ENGINE_BACKEND": engine,
            "FEEDS": {
                "memory://batch-%(batch_id)02d": {
                    "format": "jsonlines",
                    "batch_item_count": 2,
                }
            },
            "FEED_STORAGES": {"memory": ConcurrentMemoryStorage},
        },
        downloader=FeedDownloader(),
    )
    crawler.concurrent_feeds = []
    result = await crawler.crawl()
    assert result.reason == "finished"
    batches = [
        [json.loads(line)["value"] for line in feed.splitlines()]
        for feed in crawler.concurrent_feeds
    ]
    assert [len(batch) for batch in batches] == [2, 2, 1]
    assert sorted(value for batch in batches for value in batch) == [0, 1, 2, 3, 4]


async def _verify_empty_and_append(engine: str, directory: Path) -> None:
    empty_path = directory / "empty.json"
    skipped_path = directory / "skipped.json"
    await _crawl(
        EmptySpider,
        engine,
        {
            "FEEDS": {
                empty_path: {"format": "json", "overwrite": True},
                skipped_path: {
                    "format": "json",
                    "store_empty": False,
                    "overwrite": True,
                },
            }
        },
    )
    assert json.loads(empty_path.read_text()) == []
    assert not skipped_path.exists()

    append_path = directory / "append.jl"
    append_path.write_text('{"existing": true}\n')
    await _crawl(
        FormatSpider,
        engine,
        {
            "FEEDS": {
                append_path: {
                    "format": "jsonlines",
                    "fields": ["value"],
                    "overwrite": False,
                }
            }
        },
    )
    lines = [json.loads(line) for line in append_path.read_text().splitlines()]
    assert lines == [{"existing": True}, {"value": 1}, {"value": 2}]

    await _crawl(
        FormatSpider,
        engine,
        {
            "FEEDS": {
                append_path: {
                    "format": "jsonlines",
                    "fields": ["name"],
                    "overwrite": True,
                }
            }
        },
    )
    lines = [json.loads(line) for line in append_path.read_text().splitlines()]
    assert lines == [{"name": "café-1"}, {"name": "café-2"}]


async def _verify_filters_and_custom_components(engine: str, directory: Path) -> None:
    class_path = directory / "products.jl"
    custom_filter_path = directory / "minimum.jl"
    crawler = await _crawl(
        FilterSpider,
        engine,
        {
            "FEEDS": {
                class_path: {
                    "format": "jsonlines",
                    "item_classes": [Product],
                    "overwrite": True,
                },
                custom_filter_path: {
                    "format": "jsonlines",
                    "item_filter": MinimumPriceFilter,
                    "minimum_price": 10,
                    "overwrite": True,
                },
                "memory://upper": {
                    "format": "upper",
                    "item_filter": MinimumPriceFilter,
                    "minimum_price": 10,
                    "fields": ["name"],
                },
            },
            "FEED_STORAGES": {"memory": MemoryStorage},
            "FEED_EXPORTERS": {"upper": UpperLinesExporter},
        },
    )
    assert [json.loads(line) for line in class_path.read_text().splitlines()] == [
        {"name": "kept", "price": 10}
    ]
    assert [json.loads(line) for line in custom_filter_path.read_text().splitlines()] == [
        {"name": "kept", "price": 10}
    ]
    assert crawler.custom_filter_loaded is True
    assert crawler.custom_exporter_loaded is True
    assert crawler.stored_feed == ("memory://upper", b"KEPT\n")
    assert crawler.stats.get_value("feedexport/success_count/MemoryStorage") == 1


async def _verify_storage_failure(engine: str) -> None:
    previous_disable = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    try:
        crawler = await _crawl(
            FormatSpider,
            engine,
            {
                "FEEDS": {
                    "failure://items": {
                        "format": "jsonlines",
                    }
                },
                "FEED_STORAGES": {"failure": FailingStorage},
                "EXTENSIONS": {FeedSignalRecorder: 10},
            },
        )
    finally:
        logging.disable(previous_disable)
    assert crawler.stats.get_value("feedexport/failed_count/FailingStorage") == 1
    assert crawler.stats.get_value("feedexport/success_count/FailingStorage") is None
    assert crawler.feed_signal_recorder.slots == [("failure://items", 2, 1, True)]
    assert crawler.feed_signal_recorder.exporter_closed == 1


async def _verify() -> None:
    _verify_item_field_serialization()
    for engine in ("python", "rust"):
        with tempfile.TemporaryDirectory(prefix=f"spideroxide-feeds-{engine}-") as temporary:
            directory = Path(temporary)
            await _verify_formats(engine, directory / "formats")
            await _verify_batches(engine, directory / "batches")
            await _verify_concurrent_batches(engine)
            await _verify_empty_and_append(engine, directory / "storage")
            await _verify_filters_and_custom_components(engine, directory / "custom")
        await _verify_storage_failure(engine)


if __name__ == "__main__":
    asyncio.run(_verify())
    print(
        "Feed exports passed: JSON, JSON Lines, CSV, XML, fields, encoding, templates, "
        "batches, empty feeds, append and overwrite, filters, custom components, failures, "
        "signals, statistics, and engine parity"
    )
