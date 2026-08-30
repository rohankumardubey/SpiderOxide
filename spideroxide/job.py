from __future__ import annotations

import pickle
from collections.abc import Mapping

from .headers import Headers
from .http import Request
from .spider import Spider

_REQUEST_PAYLOAD_VERSION = 1


def _callback_name(spider: Spider, callback: object, field: str) -> str | None:
    if callback is None:
        return None
    name = getattr(callback, "__name__", None)
    function = getattr(callback, "__func__", None)
    owner = getattr(callback, "__self__", None)
    candidate = getattr(spider, name, None) if isinstance(name, str) else None
    if (
        owner is spider
        and function is not None
        and getattr(candidate, "__func__", None) is function
    ):
        return name
    raise ValueError(
        f"request {field} must be a bound method of the running spider when JOBDIR is set"
    )


def _resolve_callback(spider: Spider, name: object, field: str) -> object:
    if name is None:
        return None
    if not isinstance(name, str):
        raise ValueError(f"persisted request {field} name must be a string")
    callback = getattr(spider, name, None)
    if not callable(callback):
        raise ValueError(f"persisted request {field} {name!r} was not found on {spider!r}")
    return callback


def serialize_request(request: Request, spider: Spider) -> bytes:
    payload = {
        "version": _REQUEST_PAYLOAD_VERSION,
        "url": request.url,
        "callback": _callback_name(spider, request.callback, "callback"),
        "method": request.method,
        "headers": request.headers.to_raw_pairs(),
        "body": request.body,
        "cookies": dict(request.cookies),
        "meta": request.meta,
        "encoding": request.encoding,
        "priority": request.priority,
        "dont_filter": request.dont_filter,
        "errback": _callback_name(spider, request.errback, "errback"),
        "flags": request.flags,
        "cb_kwargs": request.cb_kwargs,
    }
    try:
        return pickle.dumps(payload, protocol=4)
    except (AttributeError, pickle.PickleError, TypeError) as error:
        raise ValueError(f"request cannot be serialized for JOBDIR: {error}") from error


def deserialize_request(payload: bytes, spider: Spider) -> Request:
    try:
        values = pickle.loads(payload)
    except (EOFError, pickle.PickleError, TypeError) as error:
        raise ValueError(f"persisted request payload cannot be decoded: {error}") from error
    if not isinstance(values, Mapping):
        raise ValueError("persisted request payload is not a mapping")
    if values.get("version") != _REQUEST_PAYLOAD_VERSION:
        raise ValueError(f"unsupported persisted request version: {values.get('version')!r}")

    raw_headers = values.get("headers")
    if not isinstance(raw_headers, list):
        raise ValueError("persisted request headers are invalid")
    headers = Headers()
    for pair in raw_headers:
        if (
            not isinstance(pair, tuple)
            or len(pair) != 2
            or not isinstance(pair[0], str)
            or not isinstance(pair[1], bytes)
        ):
            raise ValueError("persisted request contains an invalid header")
        headers.appendlist(pair[0], pair[1])

    return Request(
        url=values["url"],
        callback=_resolve_callback(spider, values.get("callback"), "callback"),
        method=values["method"],
        headers=headers,
        body=values["body"],
        cookies=values["cookies"],
        meta=values["meta"],
        encoding=values["encoding"],
        priority=values["priority"],
        dont_filter=values["dont_filter"],
        errback=_resolve_callback(spider, values.get("errback"), "errback"),
        flags=values["flags"],
        cb_kwargs=values["cb_kwargs"],
    )


def serialize_spider_state(state: object) -> bytes:
    if not isinstance(state, dict):
        raise TypeError("spider.state must be a dictionary")
    try:
        return pickle.dumps(state, protocol=4)
    except (AttributeError, pickle.PickleError, TypeError) as error:
        raise ValueError(f"spider.state cannot be serialized for JOBDIR: {error}") from error


def deserialize_spider_state(payload: bytes) -> dict[object, object]:
    try:
        state = pickle.loads(payload)
    except (EOFError, pickle.PickleError, TypeError) as error:
        raise ValueError(f"persisted spider state cannot be decoded: {error}") from error
    if not isinstance(state, dict):
        raise ValueError("persisted spider state is not a dictionary")
    return state
