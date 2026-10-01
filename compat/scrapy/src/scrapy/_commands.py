from __future__ import annotations

import argparse
import asyncio
import code
import importlib
import importlib.util
import inspect
import json
import os
import platform
import pprint
import re
import shlex
import subprocess
import sys
import tempfile
import time
import webbrowser
from collections import defaultdict
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from string import Template
from types import ModuleType
from unittest import TextTestRunner
from urllib.parse import urlparse

from itemadapter import ItemAdapter

import scrapy
from scrapy import Request, Spider
from scrapy.commands import BaseRunSpiderCommand, ScrapyCommand
from scrapy.contracts import ContractsManager
from scrapy.exceptions import UsageError
from scrapy.http import Response
from scrapy.settings import BaseSettings
from scrapy.spiderloader import SpiderLoader
from spideroxide.components import component_references, load_object
from spideroxide.utils import collect_outputs

PROJECT_FILES = {
    "scrapy.cfg": """# Automatically created by: scrapy startproject

[settings]
default = $project_name.settings

[deploy]
project = $project_name
""",
    "$project_name/__init__.py": "",
    "$project_name/items.py": """from dataclasses import dataclass


@dataclass
class ${ProjectName}Item:
    pass
""",
    "$project_name/middlewares.py": """from scrapy import signals


class ${ProjectName}SpiderMiddleware:
    @classmethod
    def from_crawler(cls, crawler):
        middleware = cls()
        crawler.signals.connect(middleware.spider_opened, signal=signals.spider_opened)
        return middleware

    def process_spider_input(self, response, spider):
        return None

    def process_spider_output(self, response, result, spider):
        yield from result

    def spider_opened(self, spider):
        spider.logger.info("Spider opened: %s", spider.name)


class ${ProjectName}DownloaderMiddleware:
    def process_request(self, request, spider):
        return None

    def process_response(self, request, response, spider):
        return response
""",
    "$project_name/pipelines.py": """class ${ProjectName}Pipeline:
    def process_item(self, item, spider):
        return item
""",
    "$project_name/settings.py": """BOT_NAME = "$project_name"

SPIDER_MODULES = ["$project_name.spiders"]
NEWSPIDER_MODULE = "$project_name.spiders"

ADDONS = {}
ROBOTSTXT_OBEY = False
CONCURRENT_REQUESTS_PER_DOMAIN = 1
DOWNLOAD_DELAY = 1
FEED_EXPORT_ENCODING = "utf-8"
""",
    "$project_name/spiders/__init__.py": "",
}

SPIDER_TEMPLATES = {
    "basic": """import scrapy


class $classname(scrapy.Spider):
    name = "$name"
    allowed_domains = ["$domain"]
    start_urls = ["$url"]

    def parse(self, response):
        pass
""",
    "crawl": """import scrapy
from scrapy.linkextractors import LinkExtractor
from scrapy.spiders import CrawlSpider, Rule


class $classname(CrawlSpider):
    name = "$name"
    allowed_domains = ["$domain"]
    start_urls = ["$url"]
    rules = (Rule(LinkExtractor(allow=r"Items/"), callback="parse_item", follow=True),)

    def parse_item(self, response):
        return {}
""",
    "csvfeed": """from scrapy.spiders import CSVFeedSpider


class $classname(CSVFeedSpider):
    name = "$name"
    allowed_domains = ["$domain"]
    start_urls = ["$url"]

    def parse_row(self, response, row):
        return {}
""",
    "xmlfeed": """from scrapy.spiders import XMLFeedSpider


class $classname(XMLFeedSpider):
    name = "$name"
    allowed_domains = ["$domain"]
    start_urls = ["$url"]
    iterator = "iternodes"
    itertag = "item"

    def parse_node(self, response, selector):
        return {}
""",
}


def _camelcase(value: str) -> str:
    return "".join(part.capitalize() for part in re.split(r"[_\-\s]+", value) if part)


def _sanitize_module_name(value: str) -> str:
    value = value.replace("-", "_").replace(".", "_")
    return value if value[:1].isalpha() else f"a{value}"


def _verify_url(value: str) -> str:
    parsed = urlparse(value)
    if not parsed.scheme and not parsed.netloc:
        parsed = urlparse(f"//{value}")._replace(scheme="https")
    return parsed.geturl()


def _is_url(value: str) -> bool:
    return urlparse(value).scheme in {"http", "https", "data", "file", "ftp"}


def _process(command: ScrapyCommand) -> object:
    assert command.crawler_process is not None
    return command.crawler_process


def _finish_task(command: ScrapyCommand, task: asyncio.Task[object]) -> object | None:
    process = _process(command)
    process.start()
    if process.bootstrap_failed or task.cancelled():
        command.exitcode = 1
        return None
    error = task.exception()
    if error is not None:
        command.exitcode = 1
        raise error
    return task.result()


def _spider_classes(module: ModuleType) -> list[type[Spider]]:
    return [
        value
        for value in vars(module).values()
        if (
            inspect.isclass(value)
            and issubclass(value, Spider)
            and value is not Spider
            and value.__module__ == module.__name__
            and getattr(value, "name", None)
        )
    ]


def _import_file(path: Path) -> ModuleType:
    resolved = path.resolve()
    if resolved.suffix not in {".py", ".pyw"}:
        raise ValueError(f"Not a Python source file: {resolved}")
    module_name = f"_scrapy_runspider_{resolved.stem}_{abs(hash(resolved))}"
    spec = importlib.util.spec_from_file_location(module_name, resolved)
    if spec is None or spec.loader is None:
        raise ImportError(f"Unable to load {resolved}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _select_spider(process: object, url: str, name: str | None) -> type[Spider]:
    loader = process.spider_loader
    if name:
        return loader.load(name)
    matches = loader.find_by_request(Request(url))
    return loader.load(matches[0]) if matches else Spider


def _capture_response(
    command: ScrapyCommand,
    url: str,
    *,
    spider_name: str | None = None,
    no_redirect: bool = False,
) -> tuple[Response | None, object, Spider | None]:
    process = _process(command)
    base = _select_spider(process, url, spider_name)
    captured: list[Response] = []
    spiders: list[Spider] = []

    class CommandSpider(base):
        name = getattr(base, "name", None) or "default"

        @classmethod
        def from_crawler(cls, crawler: object, *args: object, **kwargs: object) -> Spider:
            spider = super().from_crawler(crawler, *args, **kwargs)
            spiders.append(spider)
            return spider

        async def start(self) -> object:
            meta = {"handle_httpstatus_all": True} if no_redirect else {}
            yield Request(
                url,
                callback=self._capture,
                dont_filter=True,
                meta=meta,
            )

        def _capture(self, response: Response) -> None:
            captured.append(response)

    crawler = process.create_crawler(CommandSpider)
    task = process.crawl(crawler)
    _finish_task(command, task)
    return (captured[0] if captured else None, crawler, spiders[0] if spiders else None)


def _edit_file(editor: str, path: Path) -> int:
    return subprocess.call([*shlex.split(editor), os.fspath(path)])


class StartProjectCommand(ScrapyCommand):
    requires_crawler_process = False
    default_settings = {"LOG_ENABLED": False}

    def syntax(self) -> str:
        return "<project_name> [project_dir]"

    def short_desc(self) -> str:
        return "Create new project"

    def run(self, args: list[str], opts: argparse.Namespace) -> None:
        if len(args) not in {1, 2}:
            raise UsageError
        project_name = args[0]
        project_dir = Path(args[-1]).resolve()
        if not re.fullmatch(r"[_a-zA-Z]\w*", project_name):
            print(
                "Error: Project names must begin with a letter and contain only\n"
                "letters, numbers and underscores"
            )
            self.exitcode = 1
            return
        if (project_dir / "scrapy.cfg").exists():
            print(f"Error: scrapy.cfg already exists in {project_dir}")
            self.exitcode = 1
            return
        values = {
            "project_name": project_name,
            "ProjectName": _camelcase(project_name),
        }
        for relative, content in PROJECT_FILES.items():
            path = project_dir / Template(relative).substitute(values)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(Template(content).substitute(values), encoding="utf-8")
        print(
            f"New Scrapy project {project_name!r}, created in:\n"
            f"    {project_dir}\n\n"
            "You can start your first spider with:\n"
            f"    cd {project_dir}\n"
            "    scrapy genspider example example.com"
        )


class GenSpiderCommand(ScrapyCommand):
    requires_crawler_process = False
    default_settings = {"LOG_ENABLED": False}

    def syntax(self) -> str:
        return "[options] <name> <domain>"

    def short_desc(self) -> str:
        return "Generate new spider using pre-defined templates"

    def add_options(self, parser: argparse.ArgumentParser) -> None:
        super().add_options(parser)
        parser.add_argument("-l", "--list", action="store_true", help="List available templates")
        parser.add_argument(
            "-e", "--edit", action="store_true", help="Edit spider after creating it"
        )
        parser.add_argument(
            "-d",
            "--dump",
            metavar="TEMPLATE",
            help="Dump template to standard output",
        )
        parser.add_argument(
            "-t",
            "--template",
            default="basic",
            help="Uses a custom template, given by name or by path to a .tmpl file.",
        )
        parser.add_argument(
            "--force",
            action="store_true",
            help="If the spider already exists, overwrite it with the template",
        )

    def _template(self, value: str) -> str | None:
        path = Path(value)
        if path.is_file():
            return path.read_text(encoding="utf-8")
        name = value.removesuffix(".tmpl")
        template = SPIDER_TEMPLATES.get(name)
        if template is None:
            print(
                f"Unable to find template: {value}\n"
                'Use "scrapy genspider --list" to see all available templates.'
            )
        return template

    def run(self, args: list[str], opts: argparse.Namespace) -> None:
        if opts.list:
            print("Available templates:\n", "\n".join(f"  {name}" for name in SPIDER_TEMPLATES))
            return
        if opts.dump:
            template = self._template(opts.dump)
            if template is not None:
                print(template)
            return
        if len(args) != 2:
            raise UsageError
        assert self.settings is not None
        name, url = args
        module = _sanitize_module_name(name)
        url = _verify_url(url)
        template = self._template(opts.template)
        if template is None:
            self.exitcode = 1
            return
        target_dir = Path.cwd()
        module_name = self.settings.get("NEWSPIDER_MODULE")
        if module_name:
            spider_module = importlib.import_module(module_name)
            target_dir = Path(spider_module.__file__).parent  # type: ignore[arg-type]
        target = target_dir / f"{module}.py"
        if target.exists() and not opts.force:
            print(f"{target.resolve()} already exists")
            self.exitcode = 1
            return
        values = {
            "classname": f"{_camelcase(module)}Spider",
            "domain": urlparse(url).netloc,
            "name": name,
            "url": url,
        }
        target.write_text(Template(template).substitute(values), encoding="utf-8")
        print(f"Created spider {name!r} using template {opts.template!r} in:\n  {target}")
        if opts.edit:
            self.exitcode = _edit_file(str(self.settings["EDITOR"]), target)


class CrawlCommand(BaseRunSpiderCommand):
    requires_project = True

    def syntax(self) -> str:
        return "[options] <spider>"

    def short_desc(self) -> str:
        return "Run a spider of the current project, by name"

    def run(self, args: list[str], opts: argparse.Namespace) -> None:
        if len(args) != 1:
            if len(args) > 1:
                raise UsageError(
                    "running 'scrapy crawl' with more than one spider is not supported"
                )
            raise UsageError
        process = _process(self)
        task = process.crawl(args[0], **opts.spargs)
        _finish_task(self, task)


class RunSpiderCommand(BaseRunSpiderCommand):
    def syntax(self) -> str:
        return "[options] <spider_file>"

    def short_desc(self) -> str:
        return "Run a spider from a Python file, no project required"

    def long_desc(self) -> str:
        return "Run the spider defined in the given file"

    def run(self, args: list[str], opts: argparse.Namespace) -> None:
        if len(args) != 1:
            raise UsageError
        path = Path(args[0])
        if not path.exists():
            raise UsageError(f"File not found: {path}\n")
        try:
            classes = _spider_classes(_import_file(path))
        except (ImportError, ValueError) as error:
            raise UsageError(f"Unable to load {str(path)!r}: {error}\n") from error
        if not classes:
            raise UsageError(f"No spider found in file: {path}\n")
        process = _process(self)
        task = process.crawl(classes[-1], **opts.spargs)
        _finish_task(self, task)


class ListCommand(ScrapyCommand):
    requires_project = True
    requires_crawler_process = False
    default_settings = {"LOG_ENABLED": False}

    def short_desc(self) -> str:
        return "List available spiders"

    def run(self, args: list[str], opts: argparse.Namespace) -> None:
        assert self.settings is not None
        print("\n".join(sorted(SpiderLoader.from_settings(self.settings).list())))


class SettingsCommand(ScrapyCommand):
    requires_crawler_process = False
    default_settings = {"LOG_ENABLED": False}

    def syntax(self) -> str:
        return "[options]"

    def short_desc(self) -> str:
        return "Get settings values"

    def add_options(self, parser: argparse.ArgumentParser) -> None:
        super().add_options(parser)
        helps = {
            "get": "print raw setting value",
            "getbool": "print setting value, interpreted as a boolean",
            "getint": "print setting value, interpreted as an integer",
            "getfloat": "print setting value, interpreted as a float",
            "getlist": "print setting value, interpreted as a list",
        }
        for name, help_text in helps.items():
            parser.add_argument(f"--{name}", metavar="SETTING", help=help_text)

    def run(self, args: list[str], opts: argparse.Namespace) -> None:
        assert self.settings is not None
        for name in ("get", "getbool", "getint", "getfloat", "getlist"):
            setting = getattr(opts, name)
            if setting:
                value = getattr(self.settings, name)(setting)
                if isinstance(value, BaseSettings):
                    value = value.copy_to_dict()
                print(json.dumps(value) if isinstance(value, dict) else value)
                return


class EditCommand(ScrapyCommand):
    requires_project = True
    requires_crawler_process = False
    default_settings = {"LOG_ENABLED": False}

    def syntax(self) -> str:
        return "<spider>"

    def short_desc(self) -> str:
        return "Edit spider"

    def long_desc(self) -> str:
        return (
            "Edit a spider using the editor defined in the EDITOR environment "
            "variable or else the EDITOR setting"
        )

    def run(self, args: list[str], opts: argparse.Namespace) -> None:
        if len(args) != 1:
            raise UsageError
        assert self.settings is not None
        try:
            spider = SpiderLoader.from_settings(self.settings).load(args[0])
        except KeyError:
            print(f"Spider not found: {args[0]}", file=sys.stderr)
            self.exitcode = 1
            return
        path = Path(sys.modules[spider.__module__].__file__.replace(".pyc", ".py"))
        self.exitcode = _edit_file(str(self.settings["EDITOR"]), path)


class FetchCommand(ScrapyCommand):
    def syntax(self) -> str:
        return "[options] <url>"

    def short_desc(self) -> str:
        return "Fetch a URL using the Scrapy downloader"

    def long_desc(self) -> str:
        return (
            "Fetch a URL using the Scrapy downloader and print its content to stdout. "
            "You may want to use --nolog to disable logging"
        )

    def add_options(self, parser: argparse.ArgumentParser) -> None:
        super().add_options(parser)
        parser.add_argument("--spider", help="use this spider")
        parser.add_argument(
            "--headers",
            action="store_true",
            help="print response HTTP headers instead of body",
        )
        parser.add_argument(
            "--no-redirect",
            action="store_true",
            help="do not handle HTTP 3xx status codes and print response as-is",
        )

    def run(self, args: list[str], opts: argparse.Namespace) -> None:
        if len(args) != 1 or not _is_url(args[0]):
            raise UsageError
        response, _, _ = _capture_response(
            self,
            args[0],
            spider_name=opts.spider,
            no_redirect=opts.no_redirect,
        )
        if response is None:
            self.exitcode = 1
            return
        if opts.headers:
            assert response.request is not None
            for prefix, headers in (
                (b">", response.request.headers),
                (b"<", response.headers),
            ):
                for name, values in headers.to_scrapy_dict().items():
                    for value in values:
                        sys.stdout.buffer.write(prefix + b" " + name + b": " + value + b"\n")
        else:
            sys.stdout.buffer.write(response.body + b"\n")


class ViewCommand(FetchCommand):
    def short_desc(self) -> str:
        return "Open URL in browser, as seen by Scrapy"

    def long_desc(self) -> str:
        return "Fetch a URL using the Scrapy downloader and show its contents in a browser"

    def add_options(self, parser: argparse.ArgumentParser) -> None:
        ScrapyCommand.add_options(self, parser)
        parser.add_argument("--spider", help="use this spider")
        parser.add_argument(
            "--no-redirect",
            action="store_true",
            help="do not handle HTTP 3xx status codes and print response as-is",
        )

    def run(self, args: list[str], opts: argparse.Namespace) -> None:
        if len(args) != 1 or not _is_url(args[0]):
            raise UsageError
        response, _, _ = _capture_response(
            self,
            args[0],
            spider_name=opts.spider,
            no_redirect=opts.no_redirect,
        )
        if response is None:
            self.exitcode = 1
            return
        suffix = ".html" if b"<html" in response.body.lower() else ".txt"
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as output:
            output.write(response.body)
        webbrowser.open(Path(output.name).as_uri())


class ParseCommand(BaseRunSpiderCommand):
    requires_project = True

    def __init__(self) -> None:
        super().__init__()
        self.items: dict[int, list[object]] = {}
        self.requests: dict[int, list[Request]] = {}

    def syntax(self) -> str:
        return "[options] <url>"

    def short_desc(self) -> str:
        return "Parse URL (using its spider) and print the results"

    def add_options(self, parser: argparse.ArgumentParser) -> None:
        super().add_options(parser)
        parser.add_argument("--spider", help="use this spider without looking for one")
        parser.add_argument(
            "--pipelines",
            action="store_true",
            help="process items through pipelines",
        )
        parser.add_argument(
            "--nolinks",
            action="store_true",
            help="don't show links to follow (extracted requests)",
        )
        parser.add_argument(
            "--noitems",
            action="store_true",
            help="don't show scraped items",
        )
        parser.add_argument(
            "--nocolour",
            action="store_true",
            help="avoid using pygments to colorize the output",
        )
        parser.add_argument(
            "-r",
            "--rules",
            action="store_true",
            help="use CrawlSpider rules to discover the callback",
        )
        parser.add_argument(
            "-c",
            "--callback",
            help="use this callback for parsing, instead looking for a callback",
        )
        parser.add_argument(
            "-m",
            "--meta",
            help="inject extra meta into the Request, it must be a valid raw json string",
        )
        parser.add_argument(
            "--cbkwargs",
            help=(
                "inject extra callback kwargs into the Request, it must be a valid raw json string"
            ),
        )
        parser.add_argument(
            "-d",
            "--depth",
            type=int,
            default=1,
            help="maximum depth for parsing requests [default: %(default)s]",
        )
        parser.add_argument(
            "-v",
            "--verbose",
            action="store_true",
            help="print each depth level one by one",
        )

    def process_options(self, args: list[str], opts: argparse.Namespace) -> None:
        super().process_options(args, opts)
        for name in ("meta", "cbkwargs"):
            value = getattr(opts, name)
            if value:
                try:
                    setattr(opts, name, json.loads(value))
                except ValueError:
                    raise UsageError(
                        f"Invalid --{name} value, pass a valid JSON string",
                        print_help=False,
                    ) from None
            else:
                setattr(opts, name, {})

    def run(self, args: list[str], opts: argparse.Namespace) -> None:
        if len(args) != 1 or not _is_url(args[0]):
            raise UsageError
        if opts.depth <= 0:
            print("\n>>> STATUS DEPTH LEVEL 0 <<<")
            return
        process = _process(self)
        url = args[0]
        base = _select_spider(process, url, opts.spider)
        command = self

        class ParseSpider(base):
            name = getattr(base, "name", None) or "parse"

            async def start(self) -> object:
                yield Request(
                    url,
                    callback=self._cli_parse,
                    cb_kwargs=dict(opts.cbkwargs),
                    meta={**opts.meta, "_cli_depth": 1},
                    dont_filter=True,
                )

            async def _cli_parse(self, response: Response, **cb_kwargs: object) -> list[object]:
                depth = int(response.meta.get("_cli_depth", 1))
                original = response.meta.get("_cli_callback")
                callback = original if callable(original) else None
                callback_name = opts.callback
                if callback is None and opts.rules and depth == 1:
                    for rule in getattr(self, "rules", ()):
                        if rule.link_extractor.matches(response.url):
                            callback_name = rule.callback or "parse"
                            break
                    else:
                        raise ValueError(
                            f"Cannot find a rule that matches {response.url!r} "
                            f"in spider: {self.name}"
                        )
                if callback is None:
                    callback = getattr(self, callback_name or "parse")
                outputs = await collect_outputs(callback(response, **cb_kwargs))
                items = [value for value in outputs if not isinstance(value, Request)]
                requests = [value for value in outputs if isinstance(value, Request)]
                command.items.setdefault(depth, []).extend(items)
                command.requests.setdefault(depth, []).extend(requests)
                emitted: list[object] = list(items)
                if depth < opts.depth:
                    for request in requests:
                        request.meta["_cli_depth"] = depth + 1
                        request.meta["_cli_callback"] = request.callback
                        request.callback = self._cli_parse
                        emitted.append(request)
                return emitted

        task = process.crawl(ParseSpider, **opts.spargs)
        _finish_task(self, task)
        max_depth = max((*self.items, *self.requests), default=0)
        levels = range(1, max_depth + 1) if opts.verbose else (None,)
        for level in levels:
            print(
                f"\n>>> DEPTH LEVEL: {level} <<<"
                if level is not None
                else f"\n>>> STATUS DEPTH LEVEL {max_depth} <<<"
            )
            if not opts.noitems:
                print("# Scraped Items " + "-" * 60)
                item_values = (
                    self.items.get(level, [])
                    if level is not None
                    else [item for values in self.items.values() for item in values]
                )
                pprint.pprint(
                    [
                        ItemAdapter(item).asdict() if ItemAdapter.is_item(item) else item
                        for item in item_values
                    ]
                )
            if not opts.nolinks:
                print("# Requests " + "-" * 65)
                pprint.pprint(self.requests.get(level if level is not None else max_depth, []))


class ShellCommand(ScrapyCommand):
    default_settings = {
        "LOGSTATS_INTERVAL": 0,
    }

    def syntax(self) -> str:
        return "[url|file]"

    def short_desc(self) -> str:
        return "Interactive scraping console"

    def long_desc(self) -> str:
        return (
            "Interactive console for scraping the given url or file. "
            "Use ./file.html syntax or full path for local file."
        )

    def add_options(self, parser: argparse.ArgumentParser) -> None:
        super().add_options(parser)
        parser.add_argument(
            "-c",
            dest="code",
            help="evaluate the code in the shell, print the result and exit",
        )
        parser.add_argument("--spider", help="use this spider")
        parser.add_argument(
            "--no-redirect",
            action="store_true",
            help="do not handle HTTP 3xx status codes and print response as-is",
        )

    def run(self, args: list[str], opts: argparse.Namespace) -> None:
        if len(args) > 1:
            raise UsageError
        url = args[0] if args else None
        if url and not _is_url(url):
            path = Path(url)
            if path.exists():
                url = path.resolve().as_uri()
            else:
                url = _verify_url(url)
        response = None
        crawler = None
        spider = None
        if url:
            response, crawler, spider = _capture_response(
                self,
                url,
                spider_name=opts.spider,
                no_redirect=opts.no_redirect,
            )
        namespace = {
            "crawler": crawler,
            "request": None if response is None else response.request,
            "response": response,
            "scrapy": scrapy,
            "settings": self.settings,
            "spider": spider,
        }
        if opts.code:
            try:
                result = eval(opts.code, namespace)
            except SyntaxError:
                exec(opts.code, namespace)
            else:
                if result is not None:
                    print(repr(result))
            return
        code.interact(banner="SpiderOxide Scrapy shell", local=namespace)


class CheckCommand(ScrapyCommand):
    requires_project = True
    default_settings = {"LOG_ENABLED": False}

    def syntax(self) -> str:
        return "[options] <spider>"

    def short_desc(self) -> str:
        return "Check spider contracts"

    def add_options(self, parser: argparse.ArgumentParser) -> None:
        super().add_options(parser)
        parser.add_argument(
            "-l",
            "--list",
            action="store_true",
            help="only list contracts, without checking them",
        )
        parser.add_argument(
            "-v",
            "--verbose",
            action="store_true",
            help="print contract tests for all spiders",
        )
        parser.add_argument(
            "-a",
            action="append",
            default=[],
            dest="spargs",
            metavar="NAME=VALUE",
            help="set spider argument (may be repeated)",
        )

    def process_options(self, args: list[str], opts: argparse.Namespace) -> None:
        super().process_options(args, opts)
        try:
            opts.spargs = dict(value.split("=", 1) for value in opts.spargs)
        except ValueError:
            raise UsageError(
                "Invalid -a value, use -a NAME=VALUE",
                print_help=False,
            ) from None
        assert self.settings is not None
        for setting_name in ("ITEM_PIPELINES", "FEEDS"):
            setting_value = self.settings.get(setting_name)
            if isinstance(setting_value, str):
                try:
                    setting_value = json.loads(setting_value)
                except ValueError:
                    pass
                else:
                    self.settings.set(setting_name, setting_value, priority="cmdline")
        priority = 35
        self.settings.set("ITEM_PIPELINES", {}, priority=priority)
        self.settings.set("FEEDS", {}, priority=priority)

    def run(self, args: list[str], opts: argparse.Namespace) -> None:
        assert self.settings is not None
        from scrapy.commands.check import TextTestResult

        references = component_references(
            self.settings.get_component_priority_dict_with_base("SPIDER_CONTRACTS")
        )
        manager = ContractsManager(load_object(reference) for reference in references)
        runner = TextTestRunner(verbosity=2 if opts.verbose else 1)
        result = TextTestResult(runner.stream, runner.descriptions, runner.verbosity)
        methods_by_spider: dict[str, list[str]] = defaultdict(list)
        process = _process(self)
        tasks = []
        originals = {}

        async def start(spider: Spider) -> object:
            for request in manager.from_spider(spider, result):
                if request is not None:
                    yield request

        previous_check = os.environ.get("SCRAPY_CHECK")
        os.environ["SCRAPY_CHECK"] = "true"
        try:
            for spider_name in args or process.spider_loader.list():
                spider_cls = process.spider_loader.load(spider_name)
                tested_methods = manager.tested_methods_from_spidercls(spider_cls)
                if opts.list:
                    methods_by_spider[spider_cls.name].extend(tested_methods)
                elif tested_methods:
                    originals[spider_cls] = spider_cls.start
                    spider_cls.start = start
                    tasks.append(process.crawl(spider_cls, **opts.spargs))

            if opts.list:
                print(
                    "\n".join(
                        f"{spider}\n" + "\n".join(f"  * {method}" for method in sorted(methods))
                        for spider, methods in sorted(methods_by_spider.items())
                        if methods or opts.verbose
                    )
                )
                return

            started = time.monotonic()
            process.start()
            stopped = time.monotonic()
            result.printErrors()
            result.printSummary(started, stopped)
            task_failed = any(task.cancelled() or task.exception() is not None for task in tasks)
            self.exitcode = int(
                not result.wasSuccessful() or process.bootstrap_failed or task_failed
            )
        finally:
            for spider_cls, original in originals.items():
                spider_cls.start = original
            if previous_check is None:
                os.environ.pop("SCRAPY_CHECK", None)
            else:
                os.environ["SCRAPY_CHECK"] = previous_check


class VersionCommand(ScrapyCommand):
    requires_crawler_process = False
    default_settings = {"LOG_ENABLED": False}

    def syntax(self) -> str:
        return "[-v]"

    def short_desc(self) -> str:
        return "Print Scrapy version"

    def add_options(self, parser: argparse.ArgumentParser) -> None:
        super().add_options(parser)
        parser.add_argument(
            "--verbose",
            "-v",
            action="store_true",
            help="also display twisted/python/platform info (useful for bug reports)",
        )

    def run(self, args: list[str], opts: argparse.Namespace) -> None:
        if not opts.verbose:
            print(f"Scrapy {scrapy.__version__}")
            return
        packages = ["spideroxide", "spideroxide-scrapy-compat", "lxml", "parsel", "w3lib"]
        rows = [("Scrapy", scrapy.__version__), ("Python", platform.python_version())]
        for package in packages:
            try:
                package_version = version(package)
            except PackageNotFoundError:
                package_version = "not installed"
            rows.append((package, package_version))
        rows.append(("Platform", platform.platform()))
        width = max(len(name) for name, _ in rows)
        for name, package_version in rows:
            print(f"{name:<{width}} : {package_version}")


class BenchCommand(ScrapyCommand):
    default_settings = {
        "LOG_LEVEL": "INFO",
        "LOGSTATS_INTERVAL": 1,
    }

    def short_desc(self) -> str:
        return "Run quick benchmark test"

    def run(self, args: list[str], opts: argparse.Namespace) -> None:
        class BenchSpider(Spider):
            name = "follow"

            async def start(self) -> object:
                for index in range(1000):
                    yield Request(
                        f"data:text/plain,{index}",
                        callback=self.parse,
                        dont_filter=True,
                    )

            def parse(self, response: Response) -> None:
                return None

        process = _process(self)
        started = time.monotonic()
        task = process.crawl(BenchSpider)
        result = _finish_task(self, task)
        elapsed = time.monotonic() - started
        count = 0 if result is None else result.stats.get("downloader/response_count", 0)
        print(f"{count} responses in {elapsed:.3f}s")


__all__ = [
    "BenchCommand",
    "CheckCommand",
    "CrawlCommand",
    "EditCommand",
    "FetchCommand",
    "GenSpiderCommand",
    "ListCommand",
    "ParseCommand",
    "RunSpiderCommand",
    "SettingsCommand",
    "ShellCommand",
    "StartProjectCommand",
    "VersionCommand",
    "ViewCommand",
]
