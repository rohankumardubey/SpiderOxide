from __future__ import annotations

from collections import defaultdict
from time import monotonic_ns
from typing import Any
from weakref import WeakKeyDictionary

live_refs: defaultdict[type, WeakKeyDictionary[object, int]] = defaultdict(WeakKeyDictionary)


class object_ref:
    """Base class that weakly tracks live instances for memory debugging."""

    __slots__ = ()

    def __new__(cls, *args: Any, **kwargs: Any) -> object_ref:
        instance = object.__new__(cls)
        live_refs[cls][instance] = monotonic_ns()
        return instance


__all__ = ["live_refs", "object_ref"]
