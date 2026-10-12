from __future__ import annotations

import inspect
import warnings
from collections.abc import Callable
from typing import Any

from .compathelpers import get_func_args_dict
from .exceptions import ScrapyDeprecationWarning

DEPRECATION_RULES: list[tuple[str, str]] = []


def attribute(obj: object, oldattr: str, newattr: str, version: str = "0.12") -> None:
    name = type(obj).__name__
    warnings.warn(
        f"{name}.{oldattr} attribute is deprecated and will be no longer supported "
        f"in Scrapy {version}, use {name}.{newattr} attribute instead",
        ScrapyDeprecationWarning,
        stacklevel=3,
    )


def create_deprecated_class(
    name: str,
    new_class: type,
    clsdict: dict[str, Any] | None = None,
    warn_category: type[Warning] = ScrapyDeprecationWarning,
    warn_once: bool = True,
    old_class_path: str | None = None,
    new_class_path: str | None = None,
    subclass_warn_message: str = (
        "{cls} inherits from deprecated class {old}, please inherit from {new}."
    ),
    instance_warn_message: str = "{cls} is deprecated, instantiate {new} instead.",
) -> type:
    def class_path(cls: type, override: str | None = None) -> str:
        return override if override is not None else f"{cls.__module__}.{cls.__name__}"

    class DeprecatedMeta(type(new_class)):
        deprecated_class: type | None = None
        warned_on_subclass = False

        def __new__(
            metacls, class_name: str, bases: tuple[type, ...], attributes: dict[str, Any]
        ) -> type:
            cls = super().__new__(metacls, class_name, bases, attributes)
            if metacls.deprecated_class is None:
                metacls.deprecated_class = cls
            return cls

        def __init__(cls, class_name: str, bases: tuple[type, ...], attributes: dict[str, Any]):
            meta = type(cls)
            old = meta.deprecated_class
            if old is not None and old in bases and not (warn_once and meta.warned_on_subclass):
                meta.warned_on_subclass = True
                message = subclass_warn_message.format(
                    cls=class_path(cls),
                    old=class_path(old, old_class_path),
                    new=class_path(new_class, new_class_path),
                )
                if warn_once:
                    message += " (warning only on first subclass, there may be others)"
                warnings.warn(message, warn_category, stacklevel=2)
            super().__init__(class_name, bases, attributes)

        def __instancecheck__(cls, instance: object) -> bool:
            return any(
                cls.__subclasscheck__(candidate)
                for candidate in (type(instance), instance.__class__)
            )

        def __subclasscheck__(cls, subclass: type) -> bool:
            if cls is not DeprecatedMeta.deprecated_class:
                return super().__subclasscheck__(subclass)
            if not inspect.isclass(subclass):
                raise TypeError("issubclass() arg 1 must be a class")
            return any(base in {cls, new_class} for base in subclass.__mro__)

        def __call__(cls, *args: Any, **kwargs: Any) -> Any:
            if cls is DeprecatedMeta.deprecated_class:
                warnings.warn(
                    instance_warn_message.format(
                        cls=class_path(cls, old_class_path),
                        new=class_path(new_class, new_class_path),
                    ),
                    warn_category,
                    stacklevel=2,
                )
            return super().__call__(*args, **kwargs)

    deprecated = DeprecatedMeta(name, (new_class,), clsdict or {})
    frame = inspect.currentframe()
    if frame is not None and frame.f_back is not None:
        deprecated.__module__ = frame.f_back.f_globals.get("__name__", deprecated.__module__)
    return deprecated


def update_classpath(path: Any) -> Any:
    if isinstance(path, str):
        for prefix, replacement in DEPRECATION_RULES:
            if path.startswith(prefix):
                updated = path.replace(prefix, replacement, 1)
                warnings.warn(
                    f"`{path}` class is deprecated, use `{updated}` instead",
                    ScrapyDeprecationWarning,
                    stacklevel=2,
                )
                return updated
    return path


def method_is_overridden(subclass: type, base_class: type, method_name: str) -> bool:
    base_method = getattr(base_class, method_name)
    method = getattr(subclass, method_name)
    return base_method.__code__ is not method.__code__


def argument_is_required(func: Callable[..., object], arg_name: str) -> bool:
    parameter = get_func_args_dict(func).get(arg_name)
    return parameter is not None and parameter.default is inspect.Parameter.empty


def warn_on_deprecated_spider_attribute(attribute_name: str, setting_name: str) -> None:
    warnings.warn(
        f"The '{attribute_name}' spider attribute is deprecated. "
        "Use Spider.custom_settings or Spider.update_settings() instead. "
        f"The corresponding setting name is '{setting_name}'.",
        ScrapyDeprecationWarning,
        stacklevel=2,
    )
