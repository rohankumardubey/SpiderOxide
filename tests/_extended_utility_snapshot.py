from __future__ import annotations

import ast
import asyncio
import importlib
import inspect
import json
import os
import sys
import warnings
from functools import partial
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
if os.environ.get("SPIDEROXIDE_SHIM") == "1":
    sys.path.insert(0, str(ROOT / "compat" / "scrapy" / "src"))
    sys.path.insert(1, str(ROOT / "src"))
    os.environ["SPIDEROXIDE_SCRAPY_COMPAT_ALLOW_CONFLICT"] = "1"

from scrapy import Spider
from scrapy.http import HtmlResponse, Response
from scrapy.settings import Settings
from scrapy.utils.deprecate import (
    DEPRECATION_RULES,
    argument_is_required,
    attribute,
    create_deprecated_class,
    method_is_overridden,
    update_classpath,
    warn_on_deprecated_spider_attribute,
)
from scrapy.utils.misc import (
    arg_to_iter,
    is_generator_with_return_value,
    rel_has_nofollow,
    set_environ,
    walk_callable,
    walk_modules_iter,
    warn_on_generator_with_return_value,
)
from scrapy.utils.project import inside_project, project_data_dir
from scrapy.utils.python import (
    MutableAsyncChain,
    MutableChain,
    binary_is_text,
    get_func_args,
    get_func_args_dict,
    get_spec,
    is_listlike,
    memoizemethod_noargs,
    re_rsearch,
    unique,
    without_none_values,
)
from scrapy.utils.response import (
    get_base_url,
    get_meta_refresh,
    open_in_browser,
    response_from_dict,
)


def generator_return():
    yield 1
    return 2


def generator_no_return():
    yield 1
    return None


def callable_with_defaults(self, arg: int, default: str = "ok") -> None:
    del self, arg, default


async def async_values():
    yield 2


async def chain_values() -> list[int]:
    chain = MutableAsyncChain([1])
    chain.extend(async_values(), [3])
    return [value async for value in chain]


def main() -> None:
    output: dict[str, object] = {}
    calls = []

    class Counter:
        @memoizemethod_noargs
        def value(self):
            calls.append(1)
            return len(calls)

    counter = Counter()
    output["python"] = {
        "listlike": [is_listlike(value) for value in ("str", b"str", 3, [], {}, range(2))],
        "unique": unique(["aa", "b", "ab", "c"], key=lambda word: word[0]),
        "binary": [binary_is_text(value) for value in (b"text\n", b"\0", b"\x01")],
        "args": get_func_args(callable_with_defaults, stripself=True),
        "partial": list(get_func_args_dict(partial(callable_with_defaults, default="set"))),
        "spec": get_spec(callable_with_defaults),
        "filtered": [
            without_none_values({"ok": 1, "none": None}),
            without_none_values([1, None, 2]),
            without_none_values((1, None, 2)),
        ],
        "memoized": (counter.value(), counter.value(), len(calls)),
    }
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        chain = MutableChain([1])
    chain.extend([2, 3])
    output["python"]["chain"] = list(chain)
    output["python"]["warning"] = [w.category.__name__ for w in caught]
    output["python"]["async_chain"] = asyncio.run(chain_values())
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        output["python"]["reverse_search"] = [
            re_rsearch(r"a\d", "a1 text a2"),
            re_rsearch(r"missing", "a1 text a2"),
            re_rsearch(r"$", ""),
        ]
    output["python"]["reverse_warnings"] = [w.category.__name__ for w in caught]

    class ExampleSpider(Spider):
        name = "example"

    spider = ExampleSpider()
    spider.settings = Settings({"WARN_ON_GENERATOR_RETURN_VALUE": True})
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        warn_on_generator_with_return_value(spider, generator_return)
        warn_on_generator_with_return_value(spider, generator_no_return)
    output["misc"] = {
        "single": [list(arg_to_iter(value)) for value in (None, 4, "test", {"a": 1})],
        "list": list(arg_to_iter([1, 2])),
        "nofollow": [
            rel_has_nofollow(value)
            for value in (None, "nofollow", "ugc,nofollow", " NOFOLLOW sponsored", "follow")
        ],
        "generator": [
            is_generator_with_return_value(generator_return),
            is_generator_with_return_value(generator_no_return),
        ],
        "walk": [
            type(node).__name__
            for node in walk_callable(
                ast.parse("def outer():\n  def inner():\n    return 4\n  return 3\n").body[0]
            )
            if isinstance(node, (ast.FunctionDef, ast.Return))
        ],
        "modules": [module.__name__ for module in walk_modules_iter("json.decoder")],
        "warning": [str(w.message) for w in caught],
    }

    class Base:
        def handler(self):
            return None

    class Modified(Base):
        def handler(self):
            return True

    class NewClass:
        pass

    OldClass = create_deprecated_class(
        "OldClass",
        NewClass,
        old_class_path="legacy.OldClass",
        new_class_path="modern.NewClass",
    )
    DEPRECATION_RULES.append(("legacy.", "modern."))
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        old_instance = OldClass()

        class Child(OldClass):
            pass

        class SecondChild(OldClass):
            pass

        updated = update_classpath("legacy.Component")
        attribute(old_instance, "old", "new")
        warn_on_deprecated_spider_attribute("rate", "DOWNLOAD_DELAY")
    output["deprecate"] = {
        "rules": updated,
        "identity": update_classpath(12),
        "method": [
            method_is_overridden(Base, Base, "handler"),
            method_is_overridden(Modified, Base, "handler"),
        ],
        "required": [
            argument_is_required(callable_with_defaults, key)
            for key in ("arg", "default", "missing")
        ],
        "subclass": issubclass(NewClass, OldClass) and issubclass(Child, OldClass),
        "instance": isinstance(NewClass(), OldClass) and isinstance(old_instance, OldClass),
        "child": isinstance(SecondChild(), OldClass),
        "warnings": [(str(w.message), w.category.__name__) for w in caught],
    }

    with TemporaryDirectory() as directory:
        original = Path.cwd()
        os.chdir(directory)
        try:
            with set_environ(SCRAPY_SETTINGS_MODULE="missing_spideroxide_settings_module"):
                with warnings.catch_warnings(record=True) as caught:
                    warnings.simplefilter("always")
                    missing_settings = inside_project()
            Path("scrapy.cfg").write_text("[settings]\ndefault = scrapy\n")
            data_dir = project_data_dir()
            output["project"] = {
                "missing": missing_settings,
                "warning": [w.category.__name__ for w in caught],
                "restored": os.environ.get("SCRAPY_SETTINGS_MODULE")
                != "missing_spideroxide_settings_module",
                "data_dir": data_dir == str(Path(directory) / ".scrapy"),
                "exists": Path(data_dir).is_dir(),
            }
        finally:
            os.chdir(original)

    response = HtmlResponse(
        "https://example.test/start",
        headers={"Content-Type": "text/html; charset=utf-8"},
        body=b'<!doctype html><base href="/docs/"><meta http-equiv="refresh" content="4;url=next">',
        flags=["from-cache"],
        encoding="utf-8",
    )
    data = response.to_dict()
    restored = response_from_dict(data)
    guessed = response_from_dict({key: value for key, value in data.items() if key != "_class"})
    opened: list[str] = []

    def capture(url: str) -> str:
        opened.append(url)
        return "opened"

    result = open_in_browser(response, _openfunc=capture)
    browser_file = Path(opened[0].removeprefix("file://"))
    try:
        browser_content = browser_file.read_text()
    finally:
        browser_file.unlink()
    try:
        open_in_browser(Response(response.url), _openfunc=capture)
    except TypeError as error:
        unsupported = type(error).__name__
    else:
        raise AssertionError("unsupported response type was accepted")
    output["response"] = {
        "base": get_base_url(response),
        "refresh": get_meta_refresh(response),
        "fields": sorted(data),
        "class": data["_class"],
        "flags": data["flags"],
        "restored": type(restored).__name__,
        "guessed": type(guessed).__name__,
        "open_result": result,
        "doctype_before_base": browser_content.startswith(
            '<!doctype html><base href="https://example.test/docs/">'
        ),
        "unsupported": unsupported,
    }
    public_names = {
        "python": (
            "binary_is_text",
            "get_func_args",
            "get_func_args_dict",
            "get_spec",
            "global_object_name",
            "is_listlike",
            "memoizemethod_noargs",
            "re_rsearch",
            "to_bytes",
            "to_unicode",
            "unique",
            "without_none_values",
        ),
        "misc": ("arg_to_iter", "build_from_crawler", "rel_has_nofollow", "set_environ"),
        "response": (
            "get_base_url",
            "get_meta_refresh",
            "open_in_browser",
            "response_from_dict",
            "response_status_message",
        ),
        "asyncgen": ("as_async_generator", "collect_asyncgen"),
        "asyncio": (
            "AsyncioLoopingCall",
            "CallLaterResult",
            "call_later",
            "create_looping_call",
            "is_asyncio_available",
            "run_in_thread",
            "sleep",
        ),
        "deprecate": (
            "argument_is_required",
            "attribute",
            "create_deprecated_class",
            "method_is_overridden",
            "update_classpath",
            "warn_on_deprecated_spider_attribute",
        ),
    }
    output["signatures"] = {}
    for module_name, names in public_names.items():
        module = importlib.import_module(f"scrapy.utils.{module_name}")
        for name in names:
            output["signatures"][f"{module_name}.{name}"] = [
                (
                    parameter.name,
                    parameter.kind.name,
                    parameter.default is inspect.Parameter.empty,
                )
                for parameter in inspect.signature(getattr(module, name)).parameters.values()
            ]
    print(json.dumps(output, sort_keys=True))


if __name__ == "__main__":
    main()
