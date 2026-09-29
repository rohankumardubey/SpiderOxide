from __future__ import annotations

import asyncio
import importlib
import inspect
import json
import os
import subprocess
import sys
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from types import ModuleType

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib

ROOT = Path(__file__).resolve().parents[1]
COMPAT_SRC = ROOT / "compat" / "scrapy" / "src"
SOURCE = ROOT / "src"
sys.path.insert(0, str(COMPAT_SRC))
sys.path.insert(1, str(SOURCE))
os.environ["SPIDEROXIDE_SCRAPY_COMPAT_ALLOW_CONFLICT"] = "1"

import scrapy
from itemadapter import ItemAdapter
from scrapy import Field, FormRequest, Item, Request, Selector, Spider
from scrapy.crawler import (
    AsyncCrawlerProcess,
    AsyncCrawlerRunner,
    Crawler,
    CrawlerProcess,
    CrawlerRunner,
)
from scrapy.downloadermiddlewares.retry import RetryMiddleware
from scrapy.http import Headers, HtmlResponse, JsonResponse, Response, XmlRpcRequest
from scrapy.linkextractors import IGNORED_EXTENSIONS, LinkExtractor
from scrapy.loader import ItemLoader
from scrapy.settings import SETTINGS_PRIORITIES, Settings
from scrapy.spiderloader import SpiderLoader
from scrapy.spidermiddlewares.referer import RefererMiddleware
from scrapy.spiders import CSVFeedSpider, SitemapSpider, XMLFeedSpider
from scrapy.utils.misc import load_object
from scrapy.utils.project import get_project_settings
from scrapy.utils.python import to_bytes, to_unicode
from scrapy.utils.request import RequestFingerprinter, request_from_dict, request_to_curl
from scrapy.utils.request import fingerprint as scrapy_fingerprint
from scrapy.utils.response import response_status_message
from scrapy.utils.url import url_is_from_any_domain

SIGNATURE_TARGETS = {
    "CrawlerRunner": CrawlerRunner,
    "FormRequest": FormRequest,
    "Headers": Headers,
    "Request": Request,
    "Response": Response,
    "Settings": Settings,
    "Spider": Spider,
    "fingerprint": scrapy_fingerprint,
    "request_from_dict": request_from_dict,
    "request_to_curl": request_to_curl,
}

assert issubclass(CrawlerRunner, AsyncCrawlerRunner)
assert issubclass(CrawlerProcess, AsyncCrawlerProcess)
assert Crawler.__name__ == "Crawler"
assert SpiderLoader.__name__ == "SpiderLoader"


def _api_snapshot() -> dict[str, object]:
    request = Request(
        "https://example.test/path?a=2&a=1",
        method="POST",
        headers=[("X-Test", "one")],
        body="payload",
        cookies={"session": "value"},
    )
    settings = Settings({"TEST_VALUE": 1}, priority="command")
    return {
        "constants": {
            "ignored_extensions": list(IGNORED_EXTENSIONS),
            "request_attributes": list(Request.attributes),
            "response_attributes": list(Response.attributes),
            "settings_priorities": SETTINGS_PRIORITIES,
        },
        "semantics": {
            "curl": request_to_curl(request),
            "domain_match": url_is_from_any_domain(
                "https://sub.example.test/path", ["example.test"]
            ),
            "fingerprint": scrapy_fingerprint(request).hex(),
            "header_values": [
                value.decode() for value in Headers({"X-Test": ["one", "two"]}).getlist("x-test")
            ],
            "setting_priority": settings.getpriority("TEST_VALUE"),
            "status_message": response_status_message(404),
            "to_bytes": to_bytes("SpiderOxide").decode(),
            "to_unicode": to_unicode(b"SpiderOxide"),
        },
        "signatures": {
            name: [
                [parameter_name, parameter.kind.name]
                for parameter_name, parameter in inspect.signature(target).parameters.items()
            ]
            for name, target in SIGNATURE_TARGETS.items()
        },
    }


def _verify_upstream_public_api() -> None:
    try:
        version("Scrapy")
    except PackageNotFoundError:
        return
    script = """
import inspect
import json

from scrapy import FormRequest, Request, Spider
from scrapy.crawler import CrawlerRunner
from scrapy.http import Headers, Response
from scrapy.linkextractors import IGNORED_EXTENSIONS
from scrapy.settings import SETTINGS_PRIORITIES, Settings
from scrapy.utils.python import to_bytes, to_unicode
from scrapy.utils.request import fingerprint, request_from_dict, request_to_curl
from scrapy.utils.response import response_status_message
from scrapy.utils.url import url_is_from_any_domain

targets = {
    "CrawlerRunner": CrawlerRunner,
    "FormRequest": FormRequest,
    "Headers": Headers,
    "Request": Request,
    "Response": Response,
    "Settings": Settings,
    "Spider": Spider,
    "fingerprint": fingerprint,
    "request_from_dict": request_from_dict,
    "request_to_curl": request_to_curl,
}
request = Request(
    "https://example.test/path?a=2&a=1",
    method="POST",
    headers=[("X-Test", "one")],
    body="payload",
    cookies={"session": "value"},
)
settings = Settings({"TEST_VALUE": 1}, priority="command")
snapshot = {
    "constants": {
        "ignored_extensions": list(IGNORED_EXTENSIONS),
        "request_attributes": list(Request.attributes),
        "response_attributes": list(Response.attributes),
        "settings_priorities": SETTINGS_PRIORITIES,
    },
    "semantics": {
        "curl": request_to_curl(request),
        "domain_match": url_is_from_any_domain(
            "https://sub.example.test/path", ["example.test"]
        ),
        "fingerprint": fingerprint(request).hex(),
        "header_values": [
            value.decode()
            for value in Headers({"X-Test": ["one", "two"]}).getlist("x-test")
        ],
        "setting_priority": settings.getpriority("TEST_VALUE"),
        "status_message": response_status_message(404),
        "to_bytes": to_bytes("SpiderOxide").decode(),
        "to_unicode": to_unicode(b"SpiderOxide"),
    },
    "signatures": {
        name: [
            [parameter_name, parameter.kind.name]
            for parameter_name, parameter in inspect.signature(target).parameters.items()
        ]
        for name, target in targets.items()
    },
}
print(json.dumps(snapshot, sort_keys=True))
"""
    environment = dict(os.environ)
    environment.pop("PYTHONPATH", None)
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    local_snapshot = _api_snapshot()
    upstream_snapshot = json.loads(result.stdout)
    assert local_snapshot == upstream_snapshot, json.dumps(
        {"compat": local_snapshot, "scrapy": upstream_snapshot},
        indent=2,
        sort_keys=True,
    )


def _verify_packaging_contract() -> None:
    root_config = tomllib.loads((ROOT / "pyproject.toml").read_text())
    compat_config = tomllib.loads((ROOT / "compat" / "scrapy" / "pyproject.toml").read_text())
    root_version = root_config["project"]["version"]
    compat_project = compat_config["project"]
    assert compat_project["version"] == root_version
    assert compat_project["dependencies"] == [f"spideroxide=={root_version}"]
    assert compat_project["name"] == "spideroxide-scrapy-compat"

    try:
        version("Scrapy")
    except PackageNotFoundError:
        return
    environment = dict(os.environ)
    environment.pop("SPIDEROXIDE_SCRAPY_COMPAT_ALLOW_CONFLICT", None)
    environment["PYTHONPATH"] = os.pathsep.join((str(COMPAT_SRC), str(SOURCE)))
    result = subprocess.run(
        [sys.executable, "-c", "import scrapy"],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "cannot run beside the upstream Scrapy" in result.stderr


def _verify_import_surface() -> None:
    assert Path(scrapy.__file__).resolve().is_relative_to(COMPAT_SRC)
    assert scrapy.__version__ == "2.19.0"
    assert scrapy.version_info == (2, 19, 0)
    assert scrapy.__all__ == [
        "Field",
        "FormRequest",
        "Item",
        "Request",
        "Selector",
        "Spider",
        "__version__",
        "version_info",
    ]

    module_paths = (
        "scrapy.core.downloader.handlers.datauri",
        "scrapy.core.downloader.handlers.file",
        "scrapy.core.downloader.handlers.ftp",
        "scrapy.core.downloader.handlers.http",
        "scrapy.core.downloader.handlers.s3",
        "scrapy.downloadermiddlewares.cookies",
        "scrapy.downloadermiddlewares.defaultheaders",
        "scrapy.downloadermiddlewares.downloadtimeout",
        "scrapy.downloadermiddlewares.httpauth",
        "scrapy.downloadermiddlewares.httpcompression",
        "scrapy.downloadermiddlewares.httpcache",
        "scrapy.downloadermiddlewares.httpproxy",
        "scrapy.downloadermiddlewares.offsite",
        "scrapy.downloadermiddlewares.redirect",
        "scrapy.downloadermiddlewares.retry",
        "scrapy.downloadermiddlewares.robotstxt",
        "scrapy.downloadermiddlewares.stats",
        "scrapy.exporters",
        "scrapy.extensions.feedexport",
        "scrapy.extensions.httpcache",
        "scrapy.extensions.postprocessing",
        "scrapy.http.request.form",
        "scrapy.http.request.json_request",
        "scrapy.http.response.html",
        "scrapy.http.response.text",
        "scrapy.http.response.xml",
        "scrapy.pipelines.files",
        "scrapy.pipelines.images",
        "scrapy.pipelines.media",
        "scrapy.spidermiddlewares.base",
        "scrapy.spidermiddlewares.depth",
        "scrapy.spidermiddlewares.httperror",
        "scrapy.spidermiddlewares.metacopy",
        "scrapy.spidermiddlewares.referer",
        "scrapy.spidermiddlewares.start",
        "scrapy.spidermiddlewares.urllength",
        "scrapy.spiders.feed",
        "scrapy.spiders.sitemap",
        "scrapy.utils.iterators",
        "scrapy.utils.misc",
        "scrapy.utils.project",
        "scrapy.utils.python",
        "scrapy.utils.request",
        "scrapy.utils.response",
        "scrapy.utils.sitemap",
        "scrapy.utils.spider",
        "scrapy.utils.trackref",
        "scrapy.utils.url",
    )
    for path in module_paths:
        assert importlib.import_module(path).__name__ == path

    assert load_object("scrapy.downloadermiddlewares.retry.RetryMiddleware") is RetryMiddleware
    assert load_object("scrapy.spidermiddlewares.referer.RefererMiddleware") is RefererMiddleware
    assert load_object("scrapy.squeues.LifoMemoryQueue").__name__ == "LifoMemoryQueue"
    assert load_object("scrapy.spiders.feed.XMLFeedSpider") is XMLFeedSpider
    assert load_object("scrapy.spiders.feed.CSVFeedSpider") is CSVFeedSpider
    assert load_object("scrapy.spiders.sitemap.SitemapSpider") is SitemapSpider


class Product(Item):
    name = Field()


def _verify_models_and_utilities() -> None:
    item = Product(name="SpiderOxide")
    assert ItemAdapter(item).asdict() == {"name": "SpiderOxide"}
    request = FormRequest(
        "https://example.test/form",
        formdata={"query": "rust crawler"},
        callback=None,
        headers=Headers({"X-Test": "value"}),
    )
    assert isinstance(request, Request)
    assert RequestFingerprinter().fingerprint(request)
    restored = request_from_dict(request.to_dict())
    assert isinstance(restored, FormRequest)
    assert restored.url == request.url
    assert "curl -X POST" in request_to_curl(request)
    rpc_request = XmlRpcRequest(
        "https://example.test/rpc",
        params=(("SpiderOxide",),),
    )
    assert rpc_request.method == "POST"
    assert rpc_request.dont_filter is True
    assert rpc_request.headers["Content-Type"] == b"text/xml"
    assert JsonResponse
    assert "pdf" in IGNORED_EXTENSIONS
    assert Selector(text="<h1>SpiderOxide</h1>").css("h1::text").get() == "SpiderOxide"
    assert LinkExtractor
    assert ItemLoader


def _verify_project_settings() -> None:
    module = ModuleType("example_settings")
    module.CONCURRENT_REQUESTS = 7
    module.DOWNLOAD_TIMEOUT = 12.5
    sys.modules[module.__name__] = module
    previous = os.environ.get("SCRAPY_SETTINGS_MODULE")
    previous_project = os.environ.get("SCRAPY_PROJECT")
    previous_shell = os.environ.get("SCRAPY_PYTHON_SHELL")
    os.environ["SCRAPY_SETTINGS_MODULE"] = module.__name__
    os.environ["SCRAPY_PROJECT"] = "compatibility"
    os.environ["SCRAPY_PYTHON_SHELL"] = "python"
    try:
        settings = get_project_settings()
    finally:
        if previous is None:
            os.environ.pop("SCRAPY_SETTINGS_MODULE", None)
        else:
            os.environ["SCRAPY_SETTINGS_MODULE"] = previous
        if previous_project is None:
            os.environ.pop("SCRAPY_PROJECT", None)
        else:
            os.environ["SCRAPY_PROJECT"] = previous_project
        if previous_shell is None:
            os.environ.pop("SCRAPY_PYTHON_SHELL", None)
        else:
            os.environ["SCRAPY_PYTHON_SHELL"] = previous_shell
        sys.modules.pop(module.__name__, None)
    assert settings.getint("CONCURRENT_REQUESTS") == 7
    assert settings.getfloat("DOWNLOAD_TIMEOUT") == 12.5
    assert settings["PROJECT"] == "compatibility"
    assert settings["PYTHON_SHELL"] == "python"


class RecordingDownloader:
    async def fetch(self, request: Request) -> Response:
        return HtmlResponse(
            request.url,
            body=b"<h1>compatible</h1>",
            encoding="utf-8",
            request=request,
        )

    async def close(self) -> None:
        return None


class CompatibleSpider(Spider):
    name = "compatible"

    async def start(self):
        yield Request("https://example.test/", callback=self.parse)

    def parse(self, response: HtmlResponse) -> dict[str, str]:
        return {"title": response.css("h1::text").get()}


async def _verify_unchanged_project_crawl() -> None:
    runner = CrawlerRunner(
        Settings(
            {
                "ENGINE_BACKEND": "python",
                "ROBOTSTXT_OBEY": False,
            }
        )
    )
    result = await runner.crawl(CompatibleSpider, downloader=RecordingDownloader())
    assert result.items == ({"title": "compatible"},)


async def _verify() -> None:
    _verify_packaging_contract()
    _verify_upstream_public_api()
    _verify_import_surface()
    _verify_models_and_utilities()
    _verify_project_settings()
    await _verify_unchanged_project_crawl()


if __name__ == "__main__":
    asyncio.run(_verify())
    print(
        "Scrapy namespace passed: imports, models, project settings, utilities, "
        "component paths, and unchanged project crawl"
    )
