from __future__ import annotations

import argparse
import cProfile
import importlib
import inspect
import pkgutil
import sys
from collections.abc import Sequence
from importlib.metadata import entry_points

import scrapy
from scrapy.commands import ScrapyCommand, ScrapyHelpFormatter
from scrapy.crawler import CrawlerProcess
from scrapy.exceptions import UsageError
from scrapy.utils.project import get_project_settings, inside_project

COMMAND_NAMES = (
    "bench",
    "check",
    "crawl",
    "edit",
    "fetch",
    "genspider",
    "list",
    "parse",
    "runspider",
    "settings",
    "shell",
    "startproject",
    "version",
    "view",
)


class ScrapyArgumentParser(argparse.ArgumentParser):
    def _parse_optional(
        self,
        arg_string: str,
    ) -> tuple[argparse.Action | None, str, str | None] | None:
        if arg_string.startswith("-:"):
            return None
        return super()._parse_optional(arg_string)


def _commands(settings: object, in_project: bool) -> dict[str, ScrapyCommand]:
    commands = {}
    for name in COMMAND_NAMES:
        command_type = importlib.import_module(f"scrapy.commands.{name}").Command
        command = command_type()
        if in_project or not command.requires_project:
            commands[name] = command
    for point in entry_points(group="scrapy.commands"):
        command_type = point.load()
        if not inspect.isclass(command_type):
            raise ValueError(f"Invalid entry point {point.name}")
        command = command_type()
        if in_project or not command.requires_project:
            commands[point.name] = command
    commands_module = settings["COMMANDS_MODULE"]
    if commands_module:
        module = importlib.import_module(commands_module)
        for module_info in pkgutil.iter_modules(module.__path__):
            command_type = importlib.import_module(f"{commands_module}.{module_info.name}").Command
            command = command_type()
            if in_project or not command.requires_project:
                commands[module_info.name] = command
    return commands


def _print_header(settings: object, in_project: bool) -> None:
    if in_project:
        print(f"Scrapy {scrapy.__version__} - active project: {settings['BOT_NAME']}\n")
    else:
        print(f"Scrapy {scrapy.__version__} - no active project\n")


def _print_commands(settings: object, in_project: bool) -> None:
    _print_header(settings, in_project)
    print("Usage:\n", "  scrapy <command> [options] [args]\n", "Available commands:\n")
    print(
        "\n".join(
            f"  {name:<13} {command.short_desc()}"
            for name, command in sorted(_commands(settings, in_project).items())
        )
    )
    if not in_project:
        print("\n", "  [ more ]      More commands available when run from project directory")
    print("\n", 'Use "scrapy <command> -h" to see more info about a command')


def _project_only_commands(settings: object) -> set[str]:
    return set(_commands(settings, True)) - set(_commands(settings, False))


def _command_name(arguments: list[str]) -> str | None:
    for index in range(1, len(arguments)):
        if not arguments[index].startswith("-"):
            return arguments.pop(index)
    return None


def _usage_error(
    parser: argparse.ArgumentParser,
    function: object,
    *args: object,
) -> None:
    try:
        function(*args)  # type: ignore[operator]
    except UsageError as error:
        if str(error):
            parser.error(str(error))
        if error.print_help:
            parser.print_help()
        raise SystemExit(2) from error


def execute(
    argv: Sequence[str] | None = None,
    settings: object | None = None,
) -> None:
    arguments = list(sys.argv if argv is None else argv)
    if settings is None:
        settings = get_project_settings()
    in_project = inside_project()
    command_name = _command_name(arguments)
    if command_name is None:
        _print_commands(settings, in_project)
        raise SystemExit(0)
    available = _commands(settings, in_project)
    if command_name not in available:
        _print_header(settings, in_project)
        project_only = _project_only_commands(settings)
        if command_name in project_only and not in_project:
            names = ", ".join(sorted(project_only))
            print(
                f"The {command_name} command is not available from this location.\n"
                f"These commands are only available from within a project: {names}.\n"
            )
        else:
            print(f"Unknown command: {command_name}\n")
        print('Use "scrapy" to see available commands')
        raise SystemExit(2)

    command = available[command_name]
    parser = ScrapyArgumentParser(
        formatter_class=ScrapyHelpFormatter,
        usage=f"scrapy {command_name} {command.syntax()}",
        conflict_handler="resolve",
        description=command.long_desc(),
    )
    settings.setdict(command.default_settings, priority="command")
    command.settings = settings
    command.add_options(parser)
    opts, positional = parser.parse_known_args(arguments[1:])
    _usage_error(parser, command.process_options, positional, opts)
    if command.requires_crawler_process:
        command.crawler_process = CrawlerProcess(settings)
    if opts.profile:
        profile = cProfile.Profile()
        profile.enable()
        _usage_error(parser, command.run, positional, opts)
        profile.disable()
        profile.dump_stats(opts.profile)
    else:
        _usage_error(parser, command.run, positional, opts)
    raise SystemExit(command.exitcode)


if __name__ == "__main__":
    execute()
