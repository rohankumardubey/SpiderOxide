from __future__ import annotations

import ast
import gc
import inspect
import os
import re
import sys
import textwrap
import warnings
import weakref
from collections import deque
from collections.abc import AsyncIterator, Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager
from functools import partial, wraps
from importlib import import_module
from itertools import chain
from pkgutil import iter_modules
from typing import TypeVar

from .exceptions import ScrapyDeprecationWarning

_T = TypeVar("_T")


def is_listlike(x: object) -> bool:
    return hasattr(x, "__iter__") and not isinstance(x, (str, bytes))


def unique(list_: Iterable[_T], key: Callable[[_T], object] = lambda x: x) -> list[_T]:
    seen: set[object] = set()
    result: list[_T] = []
    for item in list_:
        value = key(item)
        if value not in seen:
            seen.add(value)
            result.append(item)
    return result


def binary_is_text(data: bytes) -> bool:
    if not isinstance(data, bytes):
        raise TypeError(f"data must be bytes, got '{type(data).__name__}'")
    return all(byte not in range(32) or byte in (0, 9, 10, 13) for byte in data)


def re_rsearch(
    pattern: str | re.Pattern[str], text: str, chunk_size: int = 1024
) -> tuple[int, int] | None:
    warnings.warn(
        "re_rsearch() is deprecated and will be removed in a future Scrapy version.",
        ScrapyDeprecationWarning,
        stacklevel=2,
    )
    if isinstance(pattern, str):
        pattern = re.compile(pattern)
    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")
    offset = len(text)
    while True:
        offset = max(0, offset - chunk_size * 1024)
        matches = list(pattern.finditer(text[offset:]))
        if matches:
            start, end = matches[-1].span()
            return offset + start, offset + end
        if offset == 0:
            return None


def memoizemethod_noargs(method: Callable[..., _T]) -> Callable[..., _T]:
    cache: weakref.WeakKeyDictionary[object, _T] = weakref.WeakKeyDictionary()

    @wraps(method)
    def cached(self: object, *args: object, **kwargs: object) -> _T:
        if self not in cache:
            cache[self] = method(self, *args, **kwargs)
        return cache[self]

    return cached


def get_func_args_dict(
    func: Callable[..., object], stripself: bool = False
) -> Mapping[str, inspect.Parameter]:
    if not callable(func):
        raise TypeError(f"func must be callable, got '{type(func).__name__}'")
    try:
        if sys.version_info >= (3, 14):
            from annotationlib import Format

            parameters = inspect.signature(func, annotation_format=Format.FORWARDREF).parameters
        else:
            parameters = inspect.signature(func).parameters
    except ValueError:
        return {}
    if isinstance(func, partial) and func.keywords:
        parameters = {
            name: value for name, value in parameters.items() if name not in func.keywords
        }
    if stripself and "self" in parameters:
        return {name: value for name, value in parameters.items() if name != "self"}
    return parameters


def get_func_args(func: Callable[..., object], stripself: bool = False) -> list[str]:
    return list(get_func_args_dict(func, stripself=stripself))


def get_spec(func: Callable[..., object]) -> tuple[list[str], dict[str, object]]:
    if inspect.isfunction(func) or inspect.ismethod(func):
        spec = inspect.getfullargspec(func)
    elif callable(func):
        spec = inspect.getfullargspec(func.__call__)
    else:
        raise TypeError(f"{type(func)} is not callable")
    defaults = spec.defaults or ()
    first_default = len(spec.args) - len(defaults)
    return spec.args[:first_default], dict(zip(spec.args[first_default:], defaults, strict=False))


def garbage_collect() -> None:
    gc.collect()
    if hasattr(sys, "pypy_version_info"):
        gc.collect()


class MutableChain(Iterator[_T]):
    def __init__(self, *args: Iterable[_T]) -> None:
        warnings.warn(
            "MutableChain is deprecated and will be removed in a future Scrapy version.",
            ScrapyDeprecationWarning,
            stacklevel=2,
        )
        self.data: Iterator[_T] = chain.from_iterable(args)

    def extend(self, *iterables: Iterable[_T]) -> None:
        self.data = chain(self.data, chain.from_iterable(iterables))

    def __iter__(self) -> MutableChain[_T]:
        return self

    def __next__(self) -> _T:
        return next(self.data)


async def _async_chain(*iterables: Iterable[_T] | AsyncIterator[_T]) -> AsyncIterator[_T]:
    for iterable in iterables:
        if isinstance(iterable, AsyncIterator):
            async for value in iterable:
                yield value
        else:
            for value in iterable:
                yield value


class MutableAsyncChain(AsyncIterator[_T]):
    def __init__(self, *args: Iterable[_T] | AsyncIterator[_T]) -> None:
        self.data: AsyncIterator[_T] = _async_chain(*args)

    def extend(self, *iterables: Iterable[_T] | AsyncIterator[_T]) -> None:
        self.data = _async_chain(self.data, _async_chain(*iterables))

    def __aiter__(self) -> MutableAsyncChain[_T]:
        return self

    async def __anext__(self) -> _T:
        return await anext(self.data)


def rel_has_nofollow(rel: str | None) -> bool:
    return rel is not None and "nofollow" in rel.lower().replace(",", " ").split()


@contextmanager
def set_environ(**kwargs: str) -> Iterator[None]:
    original = {name: os.environ.get(name) for name in kwargs}
    os.environ.update(kwargs)
    try:
        yield
    finally:
        for name, value in original.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def walk_modules_iter(path: str) -> Iterator[object]:
    module = import_module(path)
    yield module
    if hasattr(module, "__path__"):
        for _, name, is_package in iter_modules(module.__path__):
            full_path = f"{path}.{name}"
            if is_package:
                yield from walk_modules_iter(full_path)
            else:
                yield import_module(full_path)


def walk_modules(path: str) -> list[object]:
    warnings.warn(
        "The scrapy.utils.misc.walk_modules function is deprecated and will be "
        "removed in a future version of Scrapy. "
        "Use scrapy.utils.misc.walk_modules_iter instead.",
        ScrapyDeprecationWarning,
        stacklevel=2,
    )
    return list(walk_modules_iter(path))


def walk_callable(node: ast.AST) -> Iterable[ast.AST]:
    pending: deque[ast.AST] = deque([node])
    visited_function = False
    while pending:
        current = pending.popleft()
        if isinstance(current, ast.FunctionDef):
            if visited_function:
                continue
            visited_function = True
        pending.extend(ast.iter_child_nodes(current))
        yield current


def is_generator_with_return_value(callable: Callable[..., object]) -> bool:
    if not inspect.isgeneratorfunction(callable):
        return False
    func = callable
    while isinstance(func, partial):
        func = func.func
    source = textwrap.dedent(inspect.getsource(func))
    tree = ast.parse(source)
    return any(
        isinstance(node, ast.Return)
        and node.value is not None
        and not (isinstance(node.value, ast.Constant) and node.value.value is None)
        for node in walk_callable(tree)
    )


def warn_on_generator_with_return_value(spider: object, callable: Callable[..., object]) -> None:
    if not spider.settings.getbool("WARN_ON_GENERATOR_RETURN_VALUE"):
        return
    try:
        if is_generator_with_return_value(callable):
            warnings.warn(
                f'The "{spider.__class__.__name__}.{callable.__name__}" method is '
                'a generator and includes a "return" statement with a value '
                "different than None. This could lead to unexpected behaviour. Please see "
                "https://docs.python.org/3/reference/simple_stmts.html#the-return-statement "
                'for details about the semantics of the "return" statement within generators',
                stacklevel=2,
            )
    except IndentationError:
        callable_name = f"{spider.__class__.__name__}.{callable.__name__}"
        warnings.warn(
            f'Unable to determine whether or not "{callable_name}" is a generator with a '
            "return value. "
            "This will not prevent your code from working, but it prevents Scrapy from detecting "
            f'potential issues in your implementation of "{callable_name}". Please, '
            "report this in the "
            "Scrapy issue tracker (https://github.com/scrapy/scrapy/issues), "
            f'including the code of "{callable_name}"',
            stacklevel=2,
        )
