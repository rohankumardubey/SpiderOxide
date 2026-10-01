from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any, ClassVar

from itemadapter import ItemAdapter

from scrapy.contracts import Contract
from scrapy.exceptions import ContractFail
from scrapy.http import Request


class UrlContract(Contract):
    name = "url"

    def adjust_request_args(self, args: dict[str, Any]) -> dict[str, Any]:
        args["url"] = self.args[0]
        return args


class CallbackKeywordArgumentsContract(Contract):
    name = "cb_kwargs"

    def adjust_request_args(self, args: dict[str, Any]) -> dict[str, Any]:
        args["cb_kwargs"] = json.loads(" ".join(self.args))
        return args


class MetadataContract(Contract):
    name = "meta"

    def adjust_request_args(self, args: dict[str, Any]) -> dict[str, Any]:
        args["meta"] = json.loads(" ".join(self.args))
        return args


class MethodContract(Contract):
    name = "method"

    def adjust_request_args(self, args: dict[str, Any]) -> dict[str, Any]:
        args["method"] = self.args[0]
        return args


class BodyContract(Contract):
    name = "body"

    def adjust_request_args(self, args: dict[str, Any]) -> dict[str, Any]:
        args["body"] = " ".join(self.args)
        return args


class HeaderContract(Contract):
    name = "header"

    def adjust_request_args(self, args: dict[str, Any]) -> dict[str, Any]:
        args["headers"] = {
            **(args.get("headers") or {}),
            self.args[0]: " ".join(self.args[1:]),
        }
        return args


class CookieContract(Contract):
    name = "cookie"

    def adjust_request_args(self, args: dict[str, Any]) -> dict[str, Any]:
        args["cookies"] = {
            **(args.get("cookies") or {}),
            self.args[0]: " ".join(self.args[1:]),
        }
        return args


class ReturnsContract(Contract):
    name = "returns"
    object_type_verifiers: ClassVar[dict[str | None, Callable[[Any], bool]]] = {
        "request": lambda value: isinstance(value, Request),
        "requests": lambda value: isinstance(value, Request),
        "item": ItemAdapter.is_item,
        "items": ItemAdapter.is_item,
    }

    def __init__(self, *args: Any, **kwargs: Any):
        super().__init__(*args, **kwargs)
        if len(self.args) not in {1, 2, 3}:
            raise ValueError(
                f"Incorrect argument quantity: expected 1, 2 or 3, got {len(self.args)}"
            )
        self.obj_name = self.args[0] or None
        self.obj_type_verifier = self.object_type_verifiers[self.obj_name]
        self.min_bound = int(self.args[1]) if len(self.args) >= 2 else 1
        self.max_bound = int(self.args[2]) if len(self.args) >= 3 else float("inf")

    def post_process(self, output: list[Any]) -> None:
        occurrences = sum(self.obj_type_verifier(value) for value in output)
        if self.min_bound <= occurrences <= self.max_bound:
            return
        expected = (
            str(self.min_bound)
            if self.min_bound == self.max_bound
            else f"{self.min_bound}..{self.max_bound}"
        )
        raise ContractFail(f"Returned {occurrences} {self.obj_name}, expected {expected}")


class ScrapesContract(Contract):
    name = "scrapes"

    def post_process(self, output: list[Any]) -> None:
        for value in output:
            if not ItemAdapter.is_item(value):
                continue
            adapter = ItemAdapter(value)
            missing = [field for field in self.args if field not in adapter]
            if missing:
                raise ContractFail(f"Missing fields: {', '.join(missing)}")


__all__ = [
    "BodyContract",
    "CallbackKeywordArgumentsContract",
    "CookieContract",
    "HeaderContract",
    "MetadataContract",
    "MethodContract",
    "ReturnsContract",
    "ScrapesContract",
    "UrlContract",
]
