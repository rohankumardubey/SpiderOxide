from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from itemadapter import ItemAdapter

from .exceptions import NotSupported
from .http import Request, Response, TextResponse
from .iterators import csviter, xmliter_lxml
from .selectors import Selector
from .spider import Spider


def _iterate_spider_output(result: object) -> Iterable[object]:
    if result is None:
        return ()
    if isinstance(result, (Request, Mapping)) or ItemAdapter.is_item(result):
        return (result,)
    if isinstance(result, Iterable) and not isinstance(result, (str, bytes)):
        return result
    return (result,)


class XMLFeedSpider(Spider):
    iterator = "iternodes"
    itertag = "item"
    namespaces: Sequence[tuple[str, str]] = ()

    def process_results(
        self,
        response: Response,
        results: Iterable[object],
    ) -> Iterable[object]:
        return results

    def adapt_response(self, response: Response) -> Response:
        return response

    def parse_node(self, response: Response, selector: Selector) -> object:
        parse_item = getattr(self, "parse_item", None)
        if parse_item is not None:
            return parse_item(response, selector)
        raise NotImplementedError

    def parse_nodes(
        self,
        response: Response,
        nodes: Iterable[Selector],
    ) -> Iterable[object]:
        for selector in nodes:
            results = _iterate_spider_output(self.parse_node(response, selector))
            yield from self.process_results(response, results)

    def _parse(self, response: Response, **kwargs: Any) -> object:
        del kwargs
        response = self.adapt_response(response)
        if self.iterator == "iternodes":
            nodes = self._iternodes(response)
        elif self.iterator in {"xml", "html"}:
            if not isinstance(response, TextResponse):
                raise ValueError("Response content isn't text")
            selector = Selector(text=response.text, type=self.iterator, base_url=response.url)
            self._register_namespaces(selector)
            nodes = selector.xpath(f"//{self.itertag}")
        else:
            raise NotSupported("Unsupported node iterator")
        return self.parse_nodes(response, nodes)

    def _iternodes(self, response: Response) -> Iterable[Selector]:
        for node in xmliter_lxml(response, self.itertag):
            self._register_namespaces(node)
            yield node

    def _register_namespaces(self, selector: Selector) -> None:
        for prefix, uri in self.namespaces:
            selector.register_namespace(prefix, uri)


class CSVFeedSpider(Spider):
    delimiter: str | None = None
    quotechar: str | None = None
    headers: list[str] | None = None

    def process_results(
        self,
        response: Response,
        results: Iterable[object],
    ) -> Iterable[object]:
        return results

    def adapt_response(self, response: Response) -> Response:
        return response

    def parse_row(self, response: Response, row: dict[str, str]) -> object:
        raise NotImplementedError

    def parse_rows(self, response: Response) -> Iterable[object]:
        for row in csviter(
            response,
            self.delimiter,
            self.headers,
            quotechar=self.quotechar,
        ):
            results = _iterate_spider_output(self.parse_row(response, row))
            yield from self.process_results(response, results)

    def _parse(self, response: Response, **kwargs: Any) -> object:
        del kwargs
        return self.parse_rows(self.adapt_response(response))


__all__ = ["CSVFeedSpider", "XMLFeedSpider"]
