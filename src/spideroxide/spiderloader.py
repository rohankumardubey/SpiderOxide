from __future__ import annotations

import importlib
import pkgutil
import warnings
from collections import defaultdict
from collections.abc import Iterable
from types import ModuleType

from .http import Request
from .settings import Settings
from .spider import Spider


def _walk_modules(module_name: str) -> Iterable[ModuleType]:
    module = importlib.import_module(module_name)
    yield module
    path = getattr(module, "__path__", None)
    if path is None:
        return
    for info in pkgutil.walk_packages(path, prefix=f"{module.__name__}."):
        yield importlib.import_module(info.name)


def _spider_classes(module: ModuleType) -> Iterable[type[Spider]]:
    for value in vars(module).values():
        if (
            isinstance(value, type)
            and issubclass(value, Spider)
            and value is not Spider
            and value.__module__ == module.__name__
            and getattr(value, "name", None)
        ):
            yield value


class SpiderLoader:
    def __init__(self, settings: Settings) -> None:
        self.spider_modules = [str(value) for value in settings.getlist("SPIDER_MODULES")]
        self.warn_only = settings.getbool("SPIDER_LOADER_WARN_ONLY")
        self._spiders: dict[str, type[Spider]] = {}
        self._found: defaultdict[str, list[tuple[str, str]]] = defaultdict(list)
        self._load_all_spiders()

    @classmethod
    def from_settings(cls, settings: Settings) -> SpiderLoader:
        return cls(settings)

    def _load_all_spiders(self) -> None:
        for module_name in self.spider_modules:
            try:
                for module in _walk_modules(module_name):
                    self._load_spiders(module)
            except (ImportError, SyntaxError):
                if not self.warn_only:
                    raise
                warnings.warn(
                    f"Could not load spiders from module {module_name!r}",
                    RuntimeWarning,
                    stacklevel=2,
                )
        duplicates = [
            f"{class_name} named {name!r} (in {module_name})"
            for name, locations in self._found.items()
            if len(locations) > 1
            for module_name, class_name in locations
        ]
        if duplicates:
            warnings.warn(
                "There are several spiders with the same name:\n\n" + "\n\n".join(duplicates),
                UserWarning,
                stacklevel=2,
            )

    def _load_spiders(self, module: ModuleType) -> None:
        for spider_class in _spider_classes(module):
            self._found[spider_class.name].append((module.__name__, spider_class.__name__))
            self._spiders[spider_class.name] = spider_class

    def load(self, spider_name: str) -> type[Spider]:
        try:
            return self._spiders[spider_name]
        except KeyError:
            raise KeyError(f"Spider not found: {spider_name}") from None

    def find_by_request(self, request: Request) -> list[str]:
        return [
            name
            for name, spider_class in self._spiders.items()
            if spider_class.handles_request(request)
        ]

    def list(self) -> list[str]:
        return list(self._spiders)


__all__ = ["SpiderLoader"]
