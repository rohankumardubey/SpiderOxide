from __future__ import annotations

import asyncio
import sys
import threading
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from spideroxide.asyncutils import (
    AsyncioLoopingCall,
    as_async_generator,
    call_later,
    collect_asyncgen,
    create_looping_call,
    is_asyncio_available,
    run_in_thread,
    sleep,
)


async def _verify() -> None:
    assert is_asyncio_available()
    assert await collect_asyncgen(as_async_generator([1, None, 3])) == [1, None, 3]

    async def items():
        for item in (4, 5):
            yield item

    assert await collect_asyncgen(as_async_generator(items())) == [4, 5]
    value, thread_id = await run_in_thread(lambda value: (value, threading.get_ident()), 7)
    assert value == 7 and thread_id != threading.get_ident()
    assert await run_in_thread(lambda value, *, scale: value * scale, 3, scale=2) == 6
    await sleep(0)

    values: list[int] = []
    done = asyncio.Event()
    handle = call_later(0, lambda value: (values.append(value), done.set()), 1)
    await asyncio.wait_for(done.wait(), 1)
    handle.cancel()
    handle.cancel()
    canceled = call_later(0.01, values.append, 2)
    canceled.cancel()
    await sleep(0.03)
    assert values == [1]

    called = asyncio.Event()
    counter = 0

    def tick() -> None:
        nonlocal counter
        counter += 1
        if counter == 3:
            called.set()
            timer.stop()

    timer = create_looping_call(tick)
    assert isinstance(timer, AsyncioLoopingCall)
    timer.start(0.01, now=False)
    assert timer.running and counter == 0
    try:
        timer.start(0.01)
    except RuntimeError:
        pass
    else:
        raise AssertionError("starting a running timer must fail")
    await asyncio.wait_for(called.wait(), 1)
    assert counter == 3 and not timer.running
    timer.start(0.01)
    assert counter == 4 and timer.running
    timer.stop()
    timer.stop()
    await sleep(0.02)
    assert counter == 4
    try:
        timer.start(0)
    except ValueError:
        pass
    else:
        raise AssertionError("a nonpositive interval must fail")

    async def invalid_tick() -> None:
        pass

    invalid = AsyncioLoopingCall(invalid_tick)
    try:
        invalid.start(1)
    except TypeError:
        assert not invalid.running
    else:
        raise AssertionError("async callbacks are not supported")
    invalid.start(0.01, now=False)
    with patch("spideroxide.asyncutils.logger.exception") as logged:
        await sleep(0.03)
    assert not invalid.running and logged.call_count == 1

    def failing_tick() -> None:
        raise ValueError("failed")

    failing = AsyncioLoopingCall(failing_tick)
    with patch("spideroxide.asyncutils.logger.exception") as logged:
        failing.start(1)
    assert not failing.running and logged.call_count == 1


if __name__ == "__main__":
    try:
        is_asyncio_available()
    except RuntimeError as error:
        assert "running asyncio loop" in str(error)
    else:
        raise AssertionError("a running event loop is required")
    asyncio.run(_verify())
    print(
        "Asyncio utilities passed: generators, scheduling, cancellation, looping calls and threads"
    )
