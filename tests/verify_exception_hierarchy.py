from __future__ import annotations

import builtins
import importlib.util
import inspect
import sys
from importlib.metadata import PackageNotFoundError, distribution
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from spideroxide import Request, Response
from spideroxide import exceptions as native
from spideroxide.httpcache import HttpCacheMiddleware
from spideroxide.retry import _exception_types
from spideroxide.settings import Settings


def _upstream_exceptions() -> object | None:
    try:
        scrapy = distribution("Scrapy")
    except PackageNotFoundError:
        return None
    assert scrapy.version.startswith("2.19."), scrapy.version
    path = scrapy.locate_file("scrapy/exceptions.py")
    spec = importlib.util.spec_from_file_location("_reference_scrapy_exceptions", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _verify_upstream_hierarchy() -> None:
    upstream = _upstream_exceptions()
    if upstream is None:
        print("Scrapy 2.19 not installed; skipping differential exception hierarchy check")
        return

    classes = {
        name: value
        for name, value in vars(upstream).items()
        if isinstance(value, type) and value.__module__ == upstream.__name__
    }
    for name, reference in classes.items():
        actual = getattr(native, name)
        assert actual.__bases__ == tuple(
            getattr(native, base.__name__, getattr(builtins, base.__name__, None))
            for base in reference.__bases__
        ), name
        assert issubclass(actual, Warning) == issubclass(reference, Warning), name
        assert issubclass(actual, AssertionError) == issubclass(reference, AssertionError), name
        assert actual.__annotations__ == reference.__annotations__, name
        assert ("__init__" in actual.__dict__) == ("__init__" in reference.__dict__), name
        if "__init__" in reference.__dict__:
            assert actual.__init__.__annotations__ == reference.__init__.__annotations__, name
            assert tuple(
                (parameter.name, parameter.kind, parameter.default)
                for parameter in inspect.signature(actual.__init__).parameters.values()
            ) == tuple(
                (parameter.name, parameter.kind, parameter.default)
                for parameter in inspect.signature(reference.__init__).parameters.values()
            ), name

    public = {name for name in classes if not name.startswith("_")}
    assert public == {
        "NotConfigured",
        "IgnoreRequest",
        "DontCloseSpider",
        "CloseSpider",
        "StopDownload",
        "DownloadConnectionRefusedError",
        "CannotResolveHostError",
        "DownloadTimeoutError",
        "DownloadCancelledError",
        "DownloadFailedError",
        "ResponseDataLossError",
        "UnsupportedURLSchemeError",
        "DropItem",
        "NotSupported",
        "UsageError",
        "ScrapyDeprecationWarning",
        "ContractFail",
    }
    for name, reference in classes.items():
        for alias, target in vars(upstream).items():
            if alias != name and target is reference:
                assert getattr(native, alias) is getattr(native, name), (name, alias)

    samples = (
        ("NotConfigured", (), {}),
        ("_InvalidOutput", ("invalid",), {}),
        ("IgnoreRequest", ("ignored",), {}),
        ("DontCloseSpider", (), {}),
        ("CloseSpider", (), {}),
        ("CloseSpider", ("finished",), {}),
        ("StopDownload", (), {}),
        ("StopDownload", (), {"fail": False}),
        ("DownloadConnectionRefusedError", ("refused",), {}),
        ("CannotResolveHostError", ("unresolved",), {}),
        ("DownloadTimeoutError", ("timeout",), {}),
        ("DownloadCancelledError", ("cancelled",), {}),
        ("DownloadFailedError", ("failed",), {}),
        ("ResponseDataLossError", ("truncated",), {}),
        ("UnsupportedURLSchemeError", ("scheme",), {}),
        ("DropItem", ("dropped",), {}),
        ("DropItem", ("dropped",), {"log_level": "ERROR"}),
        ("NotSupported", ("unsupported",), {}),
        ("UsageError", (), {}),
        ("UsageError", ("bad argument",), {"print_help": False}),
        ("ScrapyDeprecationWarning", ("deprecated",), {}),
        ("ContractFail", ("failed",), {}),
    )
    for name, args, kwargs in samples:
        actual = getattr(native, name)(*args, **kwargs)
        expected = getattr(upstream, name)(*args, **kwargs)
        assert actual.args == expected.args, name
        assert vars(actual) == vars(expected), name
        assert str(actual) == str(expected), name
    assert not hasattr(native.StopDownload(), "response")


def _verify_internal_download_handling() -> None:
    download_errors = native._DOWNLOAD_ERRORS
    assert set(download_errors) == {
        native.DownloadError,
        native.CannotResolveHostError,
        native.DownloadConnectionRefusedError,
        native.DownloadFailedError,
        native.DownloadTimeoutError,
        native.ResponseDataLossError,
        native.DownloadCancelledError,
        native.UnsupportedURLSchemeError,
    }
    assert native.DownloadError.__bases__ == (native.SpiderOxideError,)
    retry_types = _exception_types(Settings().getlist("RETRY_EXCEPTIONS"))
    assert set(retry_types) == set(download_errors)

    counted: list[str] = []
    cache = HttpCacheMiddleware(
        Settings(),
        SimpleNamespace(inc_value=counted.append),
        SimpleNamespace(),
        SimpleNamespace(),
    )
    cached = Response("https://example.test/cached", body=b"cached")
    for error_type in download_errors:
        assert issubclass(error_type, retry_types), error_type
        if error_type is not native.DownloadError:
            assert not issubclass(error_type, native.DownloadError), error_type
            assert not issubclass(error_type, native.SpiderOxideError), error_type
        request = Request("https://example.test/cached", meta={"cached_response": cached})
        recovered = cache.process_exception(request, error_type("failed"), None)
        assert recovered is not None and recovered.body == b"cached", error_type
    assert counted == ["httpcache/errorrecovery"] * len(download_errors)


if __name__ == "__main__":
    _verify_upstream_hierarchy()
    _verify_internal_download_handling()
    print("Scrapy 2.19 exception hierarchy and download handling verified")
