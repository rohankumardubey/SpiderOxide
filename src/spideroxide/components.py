from __future__ import annotations

import importlib
import inspect
import numbers
from collections.abc import Iterable, Mapping

from .exceptions import NotConfigured


def load_object(path: object) -> object:
    if not isinstance(path, str):
        if callable(path):
            return path
        raise TypeError(f"Unexpected argument type, expected string or object, got: {type(path)}")
    module_name, separator, attribute = path.rpartition(".")
    if not separator:
        raise ValueError(f"Error loading object {path!r}: not a full path")
    module = importlib.import_module(module_name)
    try:
        return getattr(module, attribute)
    except AttributeError:
        raise NameError(
            f"Module {module_name!r} doesn't define any object named {attribute!r}"
        ) from None


def build_from_crawler(
    component: type,
    crawler: object,
    /,
    *args: object,
    **kwargs: object,
) -> object:
    factory = getattr(component, "from_crawler", None)
    if factory is not None:
        instance = factory(crawler, *args, **kwargs)
        method_name = "from_crawler"
    else:
        instance = component(*args, **kwargs)
        method_name = "__new__"
    if instance is None:
        raise TypeError(f"{component.__qualname__}.{method_name} returned None")
    return instance


def build_component(reference: object, crawler: object) -> object:
    component = load_object(reference) if isinstance(reference, str) else reference
    if not inspect.isclass(component):
        return component
    return build_from_crawler(component, crawler)


def _component_entries(value: Mapping[object, object]) -> list[tuple[object, object]]:
    entries = list(value.items())
    identities = []
    for reference, priority in entries:
        if priority is not None and not isinstance(priority, numbers.Real):
            raise ValueError(
                f"Invalid value {priority} for component {reference}, "
                "please provide a real number or None instead"
            )
        if priority is None:
            continue
        identity = load_object(reference) if isinstance(reference, str) else reference
        if identity in identities:
            raise ValueError(
                f"Some paths in {list(value)!r} convert to the same object, "
                "please update your settings"
            )
        identities.append(identity)
    return entries


def component_references(value: object) -> list[object]:
    if value is None:
        return []
    if isinstance(value, Mapping):
        enabled = [
            (reference, priority)
            for reference, priority in _component_entries(value)
            if priority is not None
        ]
        return [reference for reference, _ in sorted(enabled, key=lambda entry: entry[1])]
    if isinstance(value, Iterable) and not isinstance(value, (str, bytes)):
        return list(value)
    raise TypeError("component settings must be a mapping or iterable")


def merged_component_references(base: object, custom: object) -> list[object]:
    if isinstance(base, Mapping) and isinstance(custom, Mapping):
        merged = {}
        base_paths = {}
        for reference, priority in _component_entries(base):
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
        for reference, priority in _component_entries(custom):
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
