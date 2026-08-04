from __future__ import annotations

import importlib
import inspect
from collections.abc import Iterable, Mapping

from .exceptions import NotConfigured


def load_object(path: str) -> object:
    module_name, separator, attribute = path.rpartition(".")
    if not separator:
        raise ValueError(f"component path must include a module: {path!r}")
    module = importlib.import_module(module_name)
    try:
        return getattr(module, attribute)
    except AttributeError as exc:
        raise ImportError(f"component {attribute!r} was not found in {module_name!r}") from exc


def build_component(reference: object, crawler: object) -> object:
    component = load_object(reference) if isinstance(reference, str) else reference
    if not inspect.isclass(component):
        return component
    from_crawler = getattr(component, "from_crawler", None)
    return from_crawler(crawler) if from_crawler else component()


def component_references(value: object) -> list[object]:
    if value is None:
        return []
    if isinstance(value, Mapping):
        enabled = [
            (reference, priority) for reference, priority in value.items() if priority is not None
        ]
        return [reference for reference, _ in sorted(enabled, key=lambda entry: entry[1])]
    if isinstance(value, Iterable) and not isinstance(value, (str, bytes)):
        return list(value)
    raise TypeError("component settings must be a mapping or iterable")


def build_components(value: object, crawler: object) -> list[object]:
    components = []
    for reference in component_references(value):
        try:
            components.append(build_component(reference, crawler))
        except NotConfigured:
            continue
    return components
