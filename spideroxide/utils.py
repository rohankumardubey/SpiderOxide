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


async def collect_outputs_with_error(
    value: object,
) -> tuple[list[object], Exception | None]:
    try:
        value = await maybe_await(value)
    except Exception as exception:
        return [], exception
    if value is None:
        return [], None
    if isinstance(value, AsyncIterable):
        output = []
        try:
            async for item in value:
                output.append(item)
        except Exception as exception:
            return output, exception
        return output, None
    if isinstance(value, Iterable) and not isinstance(value, (str, bytes, dict)):
        output = []
        try:
            for item in value:
                output.append(item)
        except Exception as exception:
            return output, exception
        return output, None
    return [value], None
