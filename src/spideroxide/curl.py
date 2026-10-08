from __future__ import annotations

from typing import Any

from .http import _curl_request_kwargs


def curl_to_request_kwargs(
    curl_command: str, ignore_unknown_options: bool = True
) -> dict[str, Any]:
    values = _curl_request_kwargs(
        curl_command,
        ignore_unknown_options=ignore_unknown_options,
    )
    method = str(values.pop("method", "GET")).upper()
    if values.get("body") == "":
        values.pop("body")
    return {"method": method, **values}
