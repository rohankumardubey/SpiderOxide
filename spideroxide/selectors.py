from __future__ import annotations

from typing import Any

from itemloaders import ItemLoader as BaseItemLoader
from itemloaders.processors import Compose, Identity, Join, MapCompose, SelectJmes, TakeFirst
from parsel import Selector, SelectorList

from .types import Item


class ItemLoader(BaseItemLoader):
    """Populate structured items from values, selectors, or responses."""

    default_item_class = Item
    default_selector_class = Selector

    def __init__(
        self,
        item: Any = None,
        selector: Selector | None = None,
        response: object | None = None,
        parent: BaseItemLoader | None = None,
        **context: Any,
    ) -> None:
        if selector is None and response is not None:
            if self.default_selector_class is Selector:
                selector = getattr(response, "selector", None)
            elif (
                isinstance(self.default_selector_class, type)
                and issubclass(self.default_selector_class, Selector)
                and self.default_selector_class.__init__ is Selector.__init__
            ):
                response_selector = response.selector
                selector = self.default_selector_class(
                    root=response_selector.root,
                    type=response_selector.type,
                    namespaces=response_selector.namespaces,
                )
            else:
                selector = self.default_selector_class(response)
        context["response"] = response
        super().__init__(item=item, selector=selector, parent=parent, **context)


__all__ = [
    "Compose",
    "Identity",
    "ItemLoader",
    "Join",
    "MapCompose",
    "SelectJmes",
    "Selector",
    "SelectorList",
    "TakeFirst",
]
