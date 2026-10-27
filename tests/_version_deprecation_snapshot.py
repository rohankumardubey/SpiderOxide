from __future__ import annotations

import importlib
import json
import os
import sys
import warnings
from collections.abc import Callable
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if os.environ.get("SPIDEROXIDE_SHIM") == "1":
    sys.path.insert(0, str(ROOT / "compat" / "scrapy" / "src"))
    sys.path.insert(1, str(ROOT / "src"))
    os.environ["SPIDEROXIDE_SCRAPY_COMPAT_ALLOW_CONFLICT"] = "1"

import scrapy
from scrapy.exceptions import ScrapyDeprecationWarning
from scrapy.http import FormRequest, HtmlResponse
from scrapy.utils.deprecate import (
    DEPRECATION_RULES,
    attribute,
    create_deprecated_class,
    update_classpath,
    warn_on_deprecated_spider_attribute,
)
from scrapy.utils.misc import walk_modules
from scrapy.utils.python import MutableChain, re_rsearch


def _warning(action: Callable[[], object]) -> list[tuple[str, str, str, int]]:
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        action()
    return [
        (type(entry.message).__name__, str(entry.message), Path(entry.filename).name, entry.lineno)
        for entry in caught
    ]


def _attribute() -> None:
    attribute(object(), "old", "new", version="2.20")


def _deprecated_class() -> None:
    class Replacement:
        pass

    Deprecated = create_deprecated_class(
        "Deprecated",
        Replacement,
        old_class_path="legacy.Deprecated",
        new_class_path="modern.Replacement",
    )
    Deprecated()

    class First(Deprecated):
        pass

    class Second(Deprecated):
        pass

    assert issubclass(Replacement, Deprecated) and isinstance(Replacement(), Deprecated)
    assert issubclass(Second, Deprecated)


def _class_path() -> None:
    DEPRECATION_RULES.append(("legacy.", "modern."))
    try:
        assert update_classpath("legacy.Component") == "modern.Component"
        assert update_classpath(10) == 10
    finally:
        DEPRECATION_RULES.pop()


def _form_request() -> None:
    response = HtmlResponse(
        "https://example.test/form",
        body=b'<form action="/submit"><input name="value" value="ok"></form>',
        encoding="utf-8",
    )
    request = FormRequest.from_response(response)
    assert isinstance(request, FormRequest)


def snapshot() -> dict[str, object]:
    assert scrapy.__version__ == "2.19.0" and scrapy.version_info == (2, 19, 0)
    assert ScrapyDeprecationWarning.__name__ == "ScrapyDeprecationWarning"
    return {
        "version": scrapy.__version__,
        "version_info": scrapy.version_info,
        "shim_version": getattr(scrapy, "spideroxide_compat_version", None),
        "attribute": _warning(_attribute),
        "class": _warning(_deprecated_class),
        "class_path": _warning(_class_path),
        "spider_attribute": _warning(
            lambda: warn_on_deprecated_spider_attribute("rate", "DOWNLOAD_DELAY")
        ),
        "mutable_chain": _warning(lambda: MutableChain([1])),
        "reverse_search": _warning(lambda: re_rsearch("x", "x")),
        "walk_modules": _warning(lambda: walk_modules("json.decoder")),
        "form_request": _warning(_form_request),
        "mail": _warning(lambda: importlib.import_module("scrapy.mail")),
    }


if __name__ == "__main__":
    print(json.dumps(snapshot(), sort_keys=True))
