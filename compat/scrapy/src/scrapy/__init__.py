from __future__ import annotations

import os
import sys
from importlib.metadata import PackageNotFoundError, distribution
from types import ModuleType


def _check_distribution_conflict() -> None:
    if os.environ.get("SPIDEROXIDE_SCRAPY_COMPAT_ALLOW_CONFLICT") == "1":
        return
    try:
        installed = distribution("Scrapy")
    except PackageNotFoundError:
        return
    raise RuntimeError(
        "spideroxide-scrapy-compat cannot run beside the upstream "
        f"{installed.metadata['Name']} {installed.version} distribution; uninstall Scrapy "
        "or use a separate environment"
    )


_check_distribution_conflict()


class _BootstrapItem:
    pass


class _BootstrapField(dict):
    pass


item = ModuleType("scrapy.item")
item.BaseItem = _BootstrapItem
item.Item = _BootstrapItem
item.Field = _BootstrapField
sys.modules["scrapy.item"] = item

from spideroxide import Field, FormRequest, Item, Request, Selector, Spider  # noqa: E402

from ._compat import install  # noqa: E402

__version__ = "2.19.0"
version_info = (2, 19, 0)
spideroxide_compat_version = "0.1.0"

install()

__all__ = [
    "Field",
    "FormRequest",
    "Item",
    "Request",
    "Selector",
    "Spider",
    "__version__",
    "version_info",
]
