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


def merged_component_references(base: object, custom: object) -> list[object]:
    if isinstance(base, Mapping) and isinstance(custom, Mapping):
        merged = {}
        base_paths = {}
        for reference, priority in base.items():
            if priority is None:
                continue
            identity = load_object(reference) if isinstance(reference, str) else reference
            merged[identity] = priority
            if isinstance(reference, str):
                base_paths[reference] = identity
            module = getattr(identity, "__module__", None)
            qualname = getattr(identity, "__qualname__", None)
            if module is not None and qualname is not None:
                base_paths[f"{module}.{qualname}"] = identity
        for reference, priority in custom.items():
            if priority is None:
                identity = base_paths.get(reference) if isinstance(reference, str) else reference
                if identity is not None:
                    merged.pop(identity, None)
                continue
            identity = load_object(reference) if isinstance(reference, str) else reference
            merged[identity] = priority
        return component_references(merged)
    merged_references = []
    for reference in (*component_references(base), *component_references(custom)):
        identity = load_object(reference) if isinstance(reference, str) else reference
        if identity not in merged_references:
            merged_references.append(identity)
    return merged_references


def build_components(value: object, crawler: object, *, base: object = None) -> list[object]:
    components = []
    references = (
        component_references(value) if base is None else merged_component_references(base, value)
    )
    for reference in references:
        try:
            components.append(build_component(reference, crawler))
        except NotConfigured:
            continue
    return components
