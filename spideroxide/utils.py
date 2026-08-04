from __future__ import annotations

import inspect
from collections.abc import AsyncIterable, Iterable


async def maybe_await(value: object) -> object:
    if inspect.isawaitable(value):
        return await value
    return value


async def collect_outputs(value: object) -> list[object]:
    value = await maybe_await(value)
    if value is None:
        return []
    if isinstance(value, AsyncIterable):
        return [item async for item in value]
    if isinstance(value, Iterable) and not isinstance(value, (str, bytes, dict)):
        return list(value)
    return [value]
