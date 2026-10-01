from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from importlib.metadata import entry_points
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMPAT_SRC = ROOT / "compat" / "scrapy" / "src"
SOURCE = ROOT / "src"
COMMAND = [sys.executable, "-c", "from scrapy.cmdline import execute; execute()"]
ENVIRONMENT = {
    **os.environ,
    "BROWSER": "/usr/bin/true",
    "PYTHONPATH": os.pathsep.join((str(COMPAT_SRC), str(SOURCE))),
    "SPIDEROXIDE_SCRAPY_COMPAT_ALLOW_CONFLICT": "1",
}

SPIDER = """import scrapy


class ExampleSpider(scrapy.Spider):
    name = "example"

    async def start(self):
        yield scrapy.Request("data:text/plain,ok", callback=self.parse)

    def parse(self, response):
        yield {"argument": getattr(self, "value", None), "text": response.text}
"""


def _run(
    *args: str,
    cwd: Path,
    expected: int = 0,
    text: bool = True,
) -> subprocess.CompletedProcess:
    result = subprocess.run(
        [*COMMAND, *args],
        cwd=cwd,
        env=ENVIRONMENT,
        capture_output=True,
        text=text,
        check=False,
    )
    assert result.returncode == expected, (
        f"scrapy {' '.join(args)} returned {result.returncode}, expected {expected}\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    return result


def _verify_entry_point() -> None:
    declared = [
        point
        for point in entry_points(group="console_scripts")
        if point.name == "scrapy"
        and point.value == "scrapy.cmdline:execute"
        and point.dist.name == "spideroxide-scrapy-compat"
    ]
    if declared:
        return
    config = (ROOT / "compat" / "scrapy" / "pyproject.toml").read_text(encoding="utf-8")
    assert 'scrapy = "scrapy.cmdline:execute"' in config


def _verify_global_dispatch(directory: Path) -> None:
    help_result = _run("--help", cwd=directory)
    assert "Scrapy 2.19.0 - no active project" in help_result.stdout
    assert "startproject  Create new project" in help_result.stdout
    assert "[ more ]" in help_result.stdout

    unknown = _run("unknown", cwd=directory, expected=2)
    assert "Unknown command: unknown" in unknown.stdout

    project_only = _run("crawl", cwd=directory, expected=2)
    assert "crawl command is not available from this location" in project_only.stdout
    assert "only available from within a project" in project_only.stdout

    assert _run("version", cwd=directory).stdout.strip() == "Scrapy 2.19.0"
    verbose = _run("version", "-v", cwd=directory).stdout
    assert "spideroxide" in verbose
    assert "Platform" in verbose
    assert _run("settings", "--get", "BOT_NAME", cwd=directory).stdout.strip() == "scrapybot"
    for name in (
        "bench",
        "fetch",
        "genspider",
        "runspider",
        "settings",
        "shell",
        "startproject",
        "version",
        "view",
    ):
        command_help = _run(name, "-h", cwd=directory).stdout
        assert "Usage" in command_help
        assert "Global Options" in command_help


def _verify_project_commands(directory: Path) -> None:
    created = _run("startproject", "demo", ".", cwd=directory)
    assert "New Scrapy project 'demo'" in created.stdout
    expected_files = {
        "scrapy.cfg",
        "demo/__init__.py",
        "demo/items.py",
        "demo/middlewares.py",
        "demo/pipelines.py",
        "demo/settings.py",
        "demo/spiders/__init__.py",
    }
    assert expected_files <= {
        path.relative_to(directory).as_posix() for path in directory.rglob("*") if path.is_file()
    }

    templates = _run("genspider", "--list", cwd=directory).stdout
    assert all(name in templates for name in ("basic", "crawl", "csvfeed", "xmlfeed"))
    dumped = _run("genspider", "--dump", "basic", cwd=directory).stdout
    assert "class $classname(scrapy.Spider)" in dumped

    generated = _run("genspider", "generated", "example.com", cwd=directory)
    assert "Created spider 'generated'" in generated.stdout
    generated_path = directory / "demo" / "spiders" / "generated.py"
    assert 'name = "generated"' in generated_path.read_text(encoding="utf-8")
    _run("genspider", "generated", "example.com", cwd=directory, expected=1)

    commands = directory / "demo" / "commands"
    commands.mkdir()
    (commands / "__init__.py").write_text("", encoding="utf-8")
    (commands / "hello.py").write_text(
        """from scrapy.commands import ScrapyCommand


class Command(ScrapyCommand):
    requires_crawler_process = False

    def short_desc(self):
        return "Print a greeting"

    def run(self, args, opts):
        print("hello from project")
""",
        encoding="utf-8",
    )
    with (directory / "demo" / "settings.py").open("a", encoding="utf-8") as settings:
        settings.write('\nCOMMANDS_MODULE = "demo.commands"\n')
    assert _run("hello", cwd=directory).stdout.strip() == "hello from project"
    for name in ("check", "crawl", "edit", "list", "parse"):
        command_help = _run(name, "-h", cwd=directory).stdout
        assert "Usage" in command_help
        assert "Global Options" in command_help

    spider_path = directory / "demo" / "spiders" / "example.py"
    spider_path.write_text(SPIDER, encoding="utf-8")
    assert _run("list", cwd=directory).stdout.splitlines() == ["example", "generated"]
    assert _run("settings", "--get", "BOT_NAME", cwd=directory).stdout.strip() == "demo"
    _run("edit", "-s", "EDITOR=/usr/bin/true", "example", cwd=directory)

    _run(
        "crawl",
        "example",
        "-a",
        "value=cli",
        "-O",
        "items.jsonl:jsonlines",
        cwd=directory,
    )
    exported = (directory / "items.jsonl").read_text(encoding="utf-8")
    assert '"argument": "cli"' in exported
    _run("crawl", "example", "-o", "items.jsonl:jsonlines", cwd=directory)
    assert len((directory / "items.jsonl").read_text(encoding="utf-8").splitlines()) == 2

    parsed = _run(
        "parse",
        "data:text/plain,ok",
        "--spider",
        "example",
        cwd=directory,
    ).stdout
    assert "Scraped Items" in parsed
    assert "'text': 'ok'" in parsed

    assert _run("check", cwd=directory).stderr.rstrip().endswith("OK")


def _verify_inspection_commands(directory: Path) -> None:
    fetched = _run("fetch", "data:text/plain,hello", cwd=directory, text=False)
    assert fetched.stdout.strip() == b"hello"

    shell = _run(
        "shell",
        "data:text/plain,hello",
        "-c",
        "response.text",
        cwd=directory,
    )
    assert shell.stdout.strip() == "'hello'"

    _run("view", "data:text/html,%3Ch1%3Ehello%3C/h1%3E", cwd=directory)


def _verify_runspider(directory: Path) -> None:
    outside = directory / "outside"
    outside.mkdir()
    spider_path = outside / "standalone.py"
    spider_path.write_text(SPIDER, encoding="utf-8")
    _run(
        "runspider",
        os.fspath(spider_path),
        "-o",
        "standalone.jsonl:jsonlines",
        cwd=outside,
    )
    exported = (outside / "standalone.jsonl").read_text(encoding="utf-8")
    assert '"text": "ok"' in exported


def main() -> None:
    _verify_entry_point()
    with tempfile.TemporaryDirectory(prefix="spideroxide-cli-") as temporary:
        directory = Path(temporary)
        _verify_global_dispatch(directory)
        _verify_inspection_commands(directory)
        _verify_project_commands(directory)
        _verify_runspider(directory)
    print(
        "CLI passed: dispatch, project generation, spider commands, feeds, "
        "inspection commands, and standalone execution"
    )


if __name__ == "__main__":
    main()
