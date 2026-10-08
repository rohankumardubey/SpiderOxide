from __future__ import annotations

from collections.abc import Iterable
from operator import itemgetter
from time import monotonic_ns
from types import NoneType
from typing import Any, Self
from weakref import WeakKeyDictionary

live_refs: WeakKeyDictionary[type, WeakKeyDictionary[object, int]] = WeakKeyDictionary()


class object_ref:
    """Base class that weakly tracks live instances for memory debugging."""

    __slots__ = ()

    def __new__(cls, *args: Any, **kwargs: Any) -> Self:
        instance = object.__new__(cls)
        try:
            references = live_refs[cls]
        except KeyError:
            references = live_refs[cls] = WeakKeyDictionary()
        references[instance] = monotonic_ns()
        return instance


def format_live_refs(ignore: type | tuple[type, ...] = NoneType) -> str:
    report = "Live References\n\n"
    now_ns = monotonic_ns()
    for cls, references in sorted(live_refs.items(), key=lambda pair: pair[0].__name__):
        if not references or issubclass(cls, ignore):
            continue
        oldest_ns = min(references.values())
        report += (
            f"{cls.__name__:<30} {len(references):6}   "
            f"oldest: {int((now_ns - oldest_ns) // 1e9)}s ago\n"
        )
    return report


def print_live_refs(*args: Any, **kwargs: Any) -> None:
    print(format_live_refs(*args, **kwargs))


def get_oldest(class_name: str) -> object | None:
    for cls, references in live_refs.items():
        if cls.__name__ == class_name:
            if not references:
                break
            return min(references.items(), key=itemgetter(1))[0]
    return None


def iter_all(class_name: str) -> Iterable[object]:
    for cls, references in live_refs.items():
        if cls.__name__ == class_name:
            return references.keys()
    return ()


__all__ = [
    "format_live_refs",
    "get_oldest",
    "iter_all",
    "live_refs",
    "object_ref",
    "print_live_refs",
]
