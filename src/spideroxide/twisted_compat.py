from __future__ import annotations

import asyncio
import inspect
import sys
from collections.abc import Awaitable, Callable
from functools import wraps
from typing import ParamSpec, TypeVar

ASYNCIO_REACTOR = "twisted.internet.asyncioreactor.AsyncioSelectorReactor"

_T = TypeVar("_T")
_P = ParamSpec("_P")
_users = 0
_pool_owned = False
_closing = False
_broken = False


def is_deferred(value: object) -> bool:
    module = sys.modules.get("twisted.internet.defer")
    return module is not None and isinstance(value, module.Deferred)


def is_reactor_installed() -> bool:
    return "twisted.internet.reactor" in sys.modules


def verify_installed_reactor(reactor_path: str) -> None:
    reactor = sys.modules.get("twisted.internet.reactor")
    if reactor is None:
        raise RuntimeError("No Twisted reactor is installed")
    if reactor_path != ASYNCIO_REACTOR:
        raise RuntimeError(f"SpiderOxide supports only {ASYNCIO_REACTOR}, not {reactor_path}")
    from twisted.internet.asyncioreactor import AsyncioSelectorReactor

    if type(reactor) is not AsyncioSelectorReactor:
        raise RuntimeError(
            f"Installed Twisted reactor {type(reactor).__module__}."
            f"{type(reactor).__qualname__} is incompatible with {ASYNCIO_REACTOR}"
        )


def is_asyncio_reactor_installed() -> bool:
    reactor = sys.modules.get("twisted.internet.reactor")
    if reactor is None:
        raise RuntimeError("No Twisted reactor is installed")
    from twisted.internet.asyncioreactor import AsyncioSelectorReactor

    return type(reactor) is AsyncioSelectorReactor


def verify_installed_asyncio_event_loop(event_loop_path: str) -> None:
    verify_installed_reactor(ASYNCIO_REACTOR)
    from .components import load_object

    loop_class = load_object(event_loop_path)
    reactor = sys.modules["twisted.internet.reactor"]
    if not isinstance(reactor._asyncioEventloop, loop_class):
        raise RuntimeError(f"Installed Twisted reactor loop does not match {event_loop_path}")


def install_reactor(reactor_path: str, event_loop_path: str | None = None) -> None:
    if reactor_path != ASYNCIO_REACTOR:
        raise RuntimeError(f"SpiderOxide supports only {ASYNCIO_REACTOR}, not {reactor_path}")
    loop = asyncio.get_running_loop()
    if event_loop_path is not None:
        from .components import load_object

        loop_class = load_object(event_loop_path)
        if not isinstance(loop, loop_class):
            raise RuntimeError(
                f"Running asyncio loop {type(loop).__name__} does not match {event_loop_path}"
            )
    if "twisted.internet.reactor" not in sys.modules:
        try:
            from twisted.internet import asyncioreactor
        except ImportError as error:
            raise RuntimeError("Twisted support requires spideroxide[twisted]") from error

        asyncioreactor.install(eventloop=loop)
    verify_installed_reactor(reactor_path)
    reactor = sys.modules["twisted.internet.reactor"]
    if reactor._asyncioEventloop is not loop:
        raise RuntimeError("Twisted reactor belongs to a different asyncio event loop")


def start_reactor(reactor_path: str, event_loop_path: str | None = None) -> None:
    global _users, _pool_owned

    install_reactor(reactor_path, event_loop_path)
    if _closing or _broken:
        raise RuntimeError("Twisted threadpool is shutting down or failed to shut down")
    reactor = sys.modules["twisted.internet.reactor"]
    if not _users and not reactor.running:
        pool = reactor.getThreadPool()
        if not pool.started:
            pool.start()
            _pool_owned = True
    _users += 1


async def stop_reactor() -> None:
    global _users, _pool_owned, _closing, _broken

    if _users < 1:
        raise RuntimeError("Twisted reactor has no SpiderOxide owners")
    _users -= 1
    if _users or not _pool_owned:
        return
    reactor = sys.modules["twisted.internet.reactor"]
    _closing = True
    try:
        for call in reactor.getDelayedCalls():
            if call.active():
                call.cancel()
        reactor.disconnectAll()
        # Twisted's pool shutdown hook also removes its startup/shutdown triggers
        # and clears the cached pool; stopping the pool alone leaves it unrestartable.
        await asyncio.to_thread(reactor._stopThreadPool)
        _pool_owned = False
    except BaseException:
        _broken = True
        raise
    finally:
        _closing = False


async def await_deferred(value: object) -> object:
    reactor = sys.modules.get("twisted.internet.reactor")
    if reactor is None or not is_asyncio_reactor_installed() or not (_users or reactor.running):
        raise RuntimeError(
            f"Deferred results require TWISTED_REACTOR={ASYNCIO_REACTOR} and an active crawl"
        )
    loop = asyncio.get_running_loop()
    if reactor._asyncioEventloop is not loop:
        raise RuntimeError("Deferred belongs to a reactor on a different asyncio event loop")
    from twisted.internet.defer import CancelledError as TwistedCancelledError

    try:
        return await value.asFuture(loop)
    except TwistedCancelledError as error:
        raise asyncio.CancelledError() from error


def deferred_from_coro(value: Awaitable[_T] | _T) -> object:
    if is_deferred(value) or not inspect.isawaitable(value):
        return value
    from twisted.internet.defer import Deferred

    return Deferred.fromFuture(asyncio.ensure_future(value))


def deferred_f_from_coro_f(
    function: Callable[_P, Awaitable[_T]],
) -> Callable[_P, object]:
    @wraps(function)
    def wrapped(*args: _P.args, **kwargs: _P.kwargs) -> object:
        return deferred_from_coro(function(*args, **kwargs))

    return wrapped


def deferred_to_future(value: object) -> asyncio.Future[object]:
    if not is_deferred(value):
        raise TypeError("deferred_to_future requires a Twisted Deferred")
    reactor = sys.modules.get("twisted.internet.reactor")
    loop = asyncio.get_running_loop()
    if (
        reactor is None
        or not is_asyncio_reactor_installed()
        or reactor._asyncioEventloop is not loop
    ):
        raise RuntimeError("Deferred requires an asyncio reactor on the running loop")
    return value.asFuture(loop)


def maybe_deferred_to_future(value: object) -> asyncio.Future[object]:
    return deferred_to_future(value)


def ensure_awaitable(value: object) -> Awaitable[object]:
    if is_deferred(value):
        return maybe_deferred_to_future(value)
    if inspect.isawaitable(value):
        return value

    async def completed() -> object:
        return value

    return completed()
