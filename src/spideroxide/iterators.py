from __future__ import annotations

import csv
import logging
from collections.abc import Iterator
from io import BytesIO, StringIO

from lxml import etree

from .http import Response, TextResponse
from .selectors import Selector

logger = logging.getLogger(__name__)


def _body_text(obj: Response | str | bytes) -> str:
    if isinstance(obj, TextResponse):
        return obj.text
    if isinstance(obj, Response):
        return obj.body.decode("utf-8")
    if isinstance(obj, bytes):
        return obj.decode("utf-8")
    return obj


def csviter(
    obj: Response | str | bytes,
    delimiter: str | None = None,
    headers: list[str] | None = None,
    encoding: str | None = None,
    quotechar: str | None = None,
) -> Iterator[dict[str, str]]:
    del encoding
    kwargs: dict[str, str] = {}
    if delimiter:
        kwargs["delimiter"] = delimiter
    if quotechar:
        kwargs["quotechar"] = quotechar
    rows = csv.reader(StringIO(_body_text(obj)), **kwargs)
    field_names = headers
    if not field_names:
        try:
            field_names = next(rows)
        except StopIteration:
            return
    for row in rows:
        if len(row) != len(field_names):
            logger.warning(
                "ignoring row %d (length: %d, should be: %d)",
                rows.line_num,
                len(row),
                len(field_names),
            )
            continue
        yield dict(zip(field_names, row, strict=False))


def xmliter_lxml(
    obj: Response | str | bytes,
    nodename: str,
    namespace: str | None = None,
    prefix: str = "x",
) -> Iterator[Selector]:
    if isinstance(obj, Response):
        body = obj.body
    elif isinstance(obj, str):
        body = obj.encode()
    else:
        body = obj
    tag = f"{{{namespace}}}{nodename}" if namespace else nodename
    selection = f"//{prefix}:{nodename}" if namespace else f"//{nodename}"
    resolve_namespace = namespace is None and ":" in nodename
    if resolve_namespace:
        prefix, nodename = nodename.split(":", maxsplit=1)
    events = etree.iterparse(
        BytesIO(body),
        events=("end", "start-ns"),
        resolve_entities=False,
        huge_tree=True,
    )
    for event, data in events:
        if event == "start-ns":
            if resolve_namespace:
                current_prefix, current_namespace = data
                if current_prefix == prefix:
                    namespace = current_namespace
                    resolve_namespace = False
                    selection = f"//{prefix}:{nodename}"
                    tag = f"{{{namespace}}}{nodename}"
            continue
        node = data
        if node.tag != tag:
            continue
        node_text = etree.tostring(node, encoding="unicode")
        node.clear()
        selector = Selector(text=node_text, type="xml")
        if namespace:
            selector.register_namespace(prefix, namespace)
        yield selector.xpath(selection)[0]


__all__ = ["csviter", "xmliter_lxml"]
