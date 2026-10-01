from __future__ import annotations

import inspect
import re
import sys
import warnings
from collections.abc import AsyncGenerator, Callable, Iterable
from functools import wraps
from types import CoroutineType
from typing import Any, ClassVar
from unittest import TestCase, TestResult

from scrapy.exceptions import ScrapyDeprecationWarning
from scrapy.http import Request, Response
from scrapy.utils.misc import arg_to_iter


def _is_async(callback: Callable[..., Any]) -> bool:
    return inspect.iscoroutinefunction(callback) or inspect.isasyncgenfunction(callback)


def _collect(result: Any) -> list[Any]:
    if isinstance(result, (AsyncGenerator, CoroutineType)):
        if isinstance(result, CoroutineType):
            result.close()
        raise TypeError(
            "Callbacks that return a coroutine or an asynchronous generator "
            "must be defined with async def to be supported by contracts."
        )
    return list(arg_to_iter(result))


async def _collect_async(result: Any) -> list[Any]:
    if isinstance(result, AsyncGenerator):
        return [value async for value in result]
    if isinstance(result, CoroutineType):
        return await _collect_async(await result)
    return list(arg_to_iter(result))


def _run_hook(
    process: Callable[[Any], None],
    value: Any,
    testcase: TestCase,
    results: TestResult,
) -> None:
    try:
        results.startTest(testcase)
        process(value)
        results.stopTest(testcase)
    except AssertionError:
        results.addFailure(testcase, sys.exc_info())
    except Exception:
        results.addError(testcase, sys.exc_info())
    else:
        results.addSuccess(testcase)


class Contract:
    request_cls: type[Request] | None = None
    name: str

    def __init__(self, method: Callable[..., Any], *args: Any):
        self.testcase_pre = _create_testcase(method, f"@{self.name} pre-hook")
        self.testcase_post = _create_testcase(method, f"@{self.name} post-hook")
        self.args: tuple[Any, ...] = args

    def add_pre_hook(self, request: Request, results: TestResult) -> Request:
        if hasattr(self, "pre_process"):
            callback = request.callback
            assert callback is not None
            pre_process = self.pre_process
            testcase = self.testcase_pre

            if _is_async(callback):

                @wraps(callback)
                async def async_wrapper(response: Response, **cb_kwargs: Any) -> list[Any]:
                    _run_hook(pre_process, response, testcase, results)
                    return await _collect_async(callback(response, **cb_kwargs))

                object.__setattr__(request, "callback", async_wrapper)
            else:

                @wraps(callback)
                def wrapper(response: Response, **cb_kwargs: Any) -> list[Any]:
                    _run_hook(pre_process, response, testcase, results)
                    return _collect(callback(response, **cb_kwargs))

                object.__setattr__(request, "callback", wrapper)
        return request

    def add_post_hook(self, request: Request, results: TestResult) -> Request:
        if hasattr(self, "post_process"):
            callback = request.callback
            assert callback is not None
            post_process = self.post_process
            testcase = self.testcase_post

            if _is_async(callback):

                @wraps(callback)
                async def async_wrapper(response: Response, **cb_kwargs: Any) -> list[Any]:
                    output = await _collect_async(callback(response, **cb_kwargs))
                    _run_hook(post_process, output, testcase, results)
                    return output

                object.__setattr__(request, "callback", async_wrapper)
            else:

                @wraps(callback)
                def wrapper(response: Response, **cb_kwargs: Any) -> list[Any]:
                    output = _collect(callback(response, **cb_kwargs))
                    _run_hook(post_process, output, testcase, results)
                    return output

                object.__setattr__(request, "callback", wrapper)
        return request

    def adjust_request_args(self, args: dict[str, Any]) -> dict[str, Any]:
        return args


class ContractsManager:
    contracts: ClassVar[dict[str, type[Contract]]] = {}

    def __init__(self, contracts: Iterable[type[Contract]]):
        for contract in contracts:
            if (
                contract.add_pre_hook is not Contract.add_pre_hook
                or contract.add_post_hook is not Contract.add_post_hook
            ):
                warnings.warn(
                    f"{contract.__module__}.{contract.__qualname__} overrides"
                    " Contract.add_pre_hook() or Contract.add_post_hook(), which is"
                    " deprecated. Define pre_process() or post_process() instead."
                    " Contracts that override those methods do not support"
                    " asynchronous callbacks.",
                    ScrapyDeprecationWarning,
                    stacklevel=2,
                )
            self.contracts[contract.name] = contract

    def tested_methods_from_spidercls(self, spidercls: type) -> list[str]:
        is_method = re.compile(r"^\s*@", re.MULTILINE).search
        return [
            name
            for name, value in inspect.getmembers(spidercls)
            if callable(value) and value.__doc__ and is_method(value.__doc__)
        ]

    def extract_contracts(self, method: Callable[..., Any]) -> list[Contract]:
        contracts = []
        assert method.__doc__ is not None
        for line_ in method.__doc__.split("\n"):
            line = line_.strip()
            if not line.startswith("@"):
                continue
            match = re.match(r"@(\w+)\s*(.*)", line)
            if match is None:
                continue
            name, args = match.groups()
            contracts.append(self.contracts[name](method, *re.split(r"\s+", args)))
        return contracts

    def from_spider(self, spider: object, results: TestResult) -> list[Request | None]:
        requests = []
        for method_name in self.tested_methods_from_spidercls(type(spider)):
            method = getattr(spider, method_name)
            try:
                requests.append(self.from_method(method, results))
            except Exception:
                results.addError(_create_testcase(method, "contract"), sys.exc_info())
        return requests

    def from_method(
        self,
        method: Callable[..., Any],
        results: TestResult,
    ) -> Request | None:
        contracts = self.extract_contracts(method)
        if not contracts:
            return None
        request_cls = Request
        for contract in contracts:
            if contract.request_cls is not None:
                request_cls = contract.request_cls

        signature = inspect.signature(request_cls)
        required = []
        kwargs = {}
        for name, parameter in signature.parameters.items():
            if parameter.kind not in (
                inspect.Parameter.POSITIONAL_ONLY,
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                inspect.Parameter.KEYWORD_ONLY,
            ):
                continue
            if parameter.default is inspect.Parameter.empty:
                required.append(name)
            else:
                kwargs[name] = parameter.default
        kwargs["dont_filter"] = True
        kwargs["callback"] = method
        for contract in contracts:
            kwargs = contract.adjust_request_args(kwargs)
        if not set(required).issubset(kwargs):
            return None

        request = request_cls(**kwargs)
        for contract in reversed(contracts):
            request = contract.add_pre_hook(request, results)
        for contract in contracts:
            request = contract.add_post_hook(request, results)
        self._clean_req(request, method, results)
        return request

    def _clean_req(
        self,
        request: Request,
        method: Callable[..., Any],
        results: TestResult,
    ) -> None:
        callback = request.callback
        assert callback is not None

        if _is_async(callback):

            @wraps(callback)
            async def callback_wrapper(response: Response, **cb_kwargs: Any) -> None:
                try:
                    await _collect_async(callback(response, **cb_kwargs))
                except Exception:
                    results.addError(
                        _create_testcase(method, "callback"),
                        sys.exc_info(),
                    )

        else:

            @wraps(callback)
            def callback_wrapper(response: Response, **cb_kwargs: Any) -> None:
                try:
                    _collect(callback(response, **cb_kwargs))
                except Exception:
                    results.addError(
                        _create_testcase(method, "callback"),
                        sys.exc_info(),
                    )

        def errback_wrapper(exception: object) -> None:
            if all(
                hasattr(exception, attribute)
                for attribute in ("type", "value", "getTracebackObject")
            ):
                exc_info = (
                    exception.type,
                    exception.value,
                    exception.getTracebackObject(),
                )
            else:
                assert isinstance(exception, Exception)
                exc_info = (type(exception), exception, exception.__traceback__)
            results.addError(
                _create_testcase(method, "errback"),
                exc_info,
            )

        object.__setattr__(request, "callback", callback_wrapper)
        object.__setattr__(request, "errback", errback_wrapper)


def _create_testcase(method: Callable[..., Any], desc: str) -> TestCase:
    spider = method.__self__.name

    class ContractTestCase(TestCase):
        def __str__(self) -> str:
            return f"[{spider}] {method.__name__} ({desc})"

    name = f"{spider}_{method.__name__}"
    setattr(ContractTestCase, name, lambda self: self)
    return ContractTestCase(name)


__all__ = ["Contract", "ContractsManager"]
