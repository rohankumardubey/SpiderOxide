from __future__ import annotations

from abc import ABCMeta
from collections import deque
from collections.abc import Iterator, KeysView, MutableMapping
from copy import deepcopy
from pprint import pformat
from typing import Any, NoReturn, Protocol, TypeVar

from itemadapter import ItemAdapter
from itemadapter.adapter import ScrapyItemAdapter

RequestData = tuple[str, str, bytes, int]
ItemType = TypeVar("ItemType", bound="Item")


class Field(dict[str, Any]):
    """Container for item field metadata."""


class ItemMeta(ABCMeta):
    def __new__(
        mcs,
        class_name: str,
        bases: tuple[type, ...],
        namespace: dict[str, Any],
    ) -> ItemMeta:
        declared_fields = tuple(
            (name, value) for name, value in namespace.items() if isinstance(value, Field)
        )
        class_namespace = {
            name: value for name, value in namespace.items() if not isinstance(value, Field)
        }
        cls = super().__new__(mcs, class_name, bases, class_namespace)
        type.__setattr__(cls, "_declared_fields", declared_fields)

        ordered_names: list[str] = []
        fields: dict[str, Field] = {}
        for base in reversed(cls.__mro__):
            for name, field in vars(base).get("_declared_fields", ()):
                if name not in fields:
                    ordered_names.append(name)
                fields[name] = field
        type.__setattr__(cls, "fields", {name: fields[name] for name in ordered_names})
        return cls


class Item(MutableMapping[str, Any], metaclass=ItemMeta):
    """Mapping with an explicit, metadata-aware field schema."""

    fields: dict[str, Field]
    _declared_fields: tuple[tuple[str, Field], ...]

    def __init__(self, *args: object, **kwargs: object) -> None:
        self._values: dict[str, Any] = {}
        for name, value in dict(*args, **kwargs).items():
            self[name] = value

    def __getitem__(self, name: str) -> Any:
        return self._values[name]

    def __setitem__(self, name: str, value: Any) -> None:
        if name not in self.fields:
            raise KeyError(f"{type(self).__name__} does not support field: {name}")
        self._values[name] = value

    def __delitem__(self, name: str) -> None:
        del self._values[name]

    def __iter__(self) -> Iterator[str]:
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)

    def __getattr__(self, name: str) -> NoReturn:
        if name in self.fields:
            raise AttributeError(f"Use item[{name!r}] to get field value")
        raise AttributeError(name)

    def __setattr__(self, name: str, value: Any) -> None:
        if not name.startswith("_"):
            raise AttributeError(f"Use item[{name!r}] = {value!r} to set field value")
        super().__setattr__(name, value)

    def keys(self) -> KeysView[str]:
        return self._values.keys()

    def __repr__(self) -> str:
        return pformat(dict(self))

    def copy(self: ItemType) -> ItemType:
        return type(self)(self)

    def deepcopy(self: ItemType) -> ItemType:
        return deepcopy(self)

    __hash__ = object.__hash__


class SpiderOxideItemAdapter(ScrapyItemAdapter):
    @classmethod
    def is_item(cls, item: Any) -> bool:
        return isinstance(item, Item)

    @classmethod
    def is_item_class(cls, item_class: type) -> bool:
        return issubclass(item_class, Item)


if not isinstance(ItemAdapter.ADAPTER_CLASSES, deque):
    ItemAdapter.ADAPTER_CLASSES = deque(ItemAdapter.ADAPTER_CLASSES)
for registered_adapter in tuple(ItemAdapter.ADAPTER_CLASSES):
    if (
        registered_adapter.__module__ == SpiderOxideItemAdapter.__module__
        and registered_adapter.__name__ == SpiderOxideItemAdapter.__name__
    ):
        ItemAdapter.ADAPTER_CLASSES.remove(registered_adapter)
ItemAdapter.ADAPTER_CLASSES.appendleft(SpiderOxideItemAdapter)


class FingerprintRequest(Protocol):
    url: str
    method: str
    body: bytes


class PriorityRequest(FingerprintRequest, Protocol):
    priority: int


class ScheduledRequest(PriorityRequest, Protocol):
    pass


def request_data(request: PriorityRequest) -> RequestData:
    return (request.url, request.method, bytes(request.body), request.priority)
