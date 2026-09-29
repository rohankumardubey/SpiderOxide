from __future__ import annotations

import argparse
import builtins
import logging
import os
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, ClassVar

from scrapy.exceptions import UsageError


def arglist_to_dict(values: list[str]) -> dict[str, str]:
    try:
        return dict(value.split("=", 1) for value in values)
    except ValueError:
        raise UsageError("Invalid argument, use NAME=VALUE", print_help=False) from None


class ScrapyCommand(ABC):
    requires_project = False
    requires_crawler_process = True
    crawler_process: object | None = None
    default_settings: ClassVar[dict[str, Any]] = {}
    exitcode = 0

    def __init__(self) -> None:
        self.settings = None

    def syntax(self) -> str:
        return ""

    @abstractmethod
    def short_desc(self) -> str:
        raise NotImplementedError

    def long_desc(self) -> str:
        return self.short_desc()

    def add_options(self, parser: argparse.ArgumentParser) -> None:
        assert self.settings is not None
        group = parser.add_argument_group(title="Global Options")
        group.add_argument(
            "--logfile",
            metavar="FILE",
            help="log file. if omitted stderr will be used",
        )
        group.add_argument(
            "-L",
            "--loglevel",
            metavar="LEVEL",
            help=f"log level (default: {self.settings['LOG_LEVEL']})",
        )
        group.add_argument(
            "--nolog",
            action="store_true",
            help="disable logging completely",
        )
        group.add_argument(
            "--profile",
            metavar="FILE",
            help="write python cProfile stats to FILE",
        )
        group.add_argument("--pidfile", metavar="FILE", help="write process ID to FILE")
        group.add_argument(
            "-s",
            "--set",
            action="append",
            default=[],
            metavar="NAME=VALUE",
            help="set/override setting (may be repeated)",
        )
        group.add_argument(
            "--pdb",
            action="store_true",
            help="enable pdb on failure (uses ipdb if installed)",
        )

    def process_options(self, args: list[str], opts: argparse.Namespace) -> None:
        assert self.settings is not None
        try:
            values = dict(value.split("=", 1) for value in opts.set)
        except ValueError:
            raise UsageError(
                "Invalid -s value, use -s NAME=VALUE",
                print_help=False,
            ) from None
        self.settings.setdict(values, priority="cmdline")
        if opts.logfile:
            self.settings.set("LOG_ENABLED", True, "cmdline")
            self.settings.set("LOG_FILE", opts.logfile, "cmdline")
        if opts.loglevel:
            self.settings.set("LOG_ENABLED", True, "cmdline")
            self.settings.set("LOG_LEVEL", opts.loglevel, "cmdline")
        if opts.nolog:
            self.settings.set("LOG_ENABLED", False, "cmdline")
        if opts.pidfile:
            Path(opts.pidfile).write_text(f"{os.getpid()}{os.linesep}", encoding="utf-8")
        if self.settings.getbool("LOG_ENABLED", True):
            logging.basicConfig(
                filename=self.settings.get("LOG_FILE"),
                level=getattr(logging, str(self.settings.get("LOG_LEVEL", "DEBUG")).upper()),
            )

    @abstractmethod
    def run(self, args: list[str], opts: argparse.Namespace) -> None:
        raise NotImplementedError


class BaseRunSpiderCommand(ScrapyCommand):
    def add_options(self, parser: argparse.ArgumentParser) -> None:
        super().add_options(parser)
        parser.add_argument(
            "-a",
            dest="spargs",
            action="append",
            default=[],
            metavar="NAME=VALUE",
            help="set spider argument (may be repeated)",
        )
        parser.add_argument(
            "-o",
            "--output",
            action="append",
            metavar="FILE",
            help=(
                "append scraped items to the end of FILE (use - for stdout), "
                "to define format set a colon at the end of the output URI "
                "(i.e. -o FILE:FORMAT)"
            ),
        )
        parser.add_argument(
            "-O",
            "--overwrite-output",
            action="append",
            metavar="FILE",
            help=(
                "dump scraped items into FILE, overwriting any existing file, "
                "to define format set a colon at the end of the output URI "
                "(i.e. -O FILE:FORMAT)"
            ),
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
        if opts.output and opts.overwrite_output:
            raise UsageError("Please use only one of -o/--output and -O/--overwrite-output")
        outputs = opts.overwrite_output or opts.output or []
        if outputs:
            assert self.settings is not None
            feeds = {}
            formats = set(self.settings.getwithbase("FEED_EXPORTERS"))
            for value in outputs:
                try:
                    uri, export_format = value.rsplit(":", 1)
                except ValueError:
                    uri = value
                    export_format = Path(value).suffix.removeprefix(".")
                if export_format not in formats:
                    raise UsageError(f"Unrecognized output format {export_format!r}")
                if uri == "-":
                    uri = "stdout:"
                options: dict[str, object] = {"format": export_format}
                if opts.overwrite_output:
                    options["overwrite"] = True
                feeds[uri] = options
            feeds.update(self.settings.getdict("FEEDS"))
            self.settings.set("FEEDS", feeds, "cmdline")


class ScrapyHelpFormatter(argparse.HelpFormatter):
    def _join_parts(self, part_strings: list[str]) -> str:
        parts = builtins.list(part_strings)
        if parts and parts[0].startswith("usage: "):
            parts[0] = "Usage\n=====\n  " + parts[0].removeprefix("usage: ")
        for index in reversed(
            [position for position, part in enumerate(parts) if part.endswith(":\n")]
        ):
            heading = parts[index].removesuffix(":\n").title()
            marker = "-" if heading == "Global Options" else "="
            parts[index] = heading + "\n" + marker * len(heading) + "\n"
        return super()._join_parts(parts)


__all__ = [
    "BaseRunSpiderCommand",
    "ScrapyCommand",
    "ScrapyHelpFormatter",
    "arglist_to_dict",
]
