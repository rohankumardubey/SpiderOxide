from __future__ import annotations

import asyncio
import inspect
import json
from unittest import TestResult

from scrapy import Request, Spider
from scrapy.contracts import Contract, ContractsManager
from scrapy.contracts.default import (
    BodyContract,
    CallbackKeywordArgumentsContract,
    CookieContract,
    HeaderContract,
    MetadataContract,
    MethodContract,
    ReturnsContract,
    ScrapesContract,
    UrlContract,
)
from scrapy.http import TextResponse
from scrapy.settings import Settings


class TaggedRequest(Request):
    def __init__(self, url: str, contract_tag: str | None = None, **kwargs: object):
        super().__init__(url, **kwargs)
        self.contract_tag = contract_tag


class TaggedRequestContract(Contract):
    name = "tagged"
    request_cls = TaggedRequest

    def adjust_request_args(self, args: dict[str, object]) -> dict[str, object]:
        args["contract_tag"] = "custom"
        return args


class DemoSpider(Spider):
    name = "contracts"

    def configured(self, response: TextResponse, value: str) -> dict[str, object]:
        """Build an item from a configured request.

        @url data:text/plain,configured
        @method POST
        @body name=SpiderOxide
        @header X-Test header value
        @cookie session value
        @meta {"source": "contract"}
        @cb_kwargs {"value": "callback"}
        @returns items 1 1
        @scrapes value source
        """
        return {"value": value, "source": response.meta["source"]}

    def failing(self, response: TextResponse) -> dict[str, str]:
        """Return an incomplete item.

        @url data:text/plain,failing
        @returns items 1 1
        @scrapes required
        """
        return {"other": response.text}

    async def asynchronous(self, response: TextResponse) -> dict[str, str]:
        """Return an item asynchronously.

        @url data:text/plain,async
        @returns items 1 1
        @scrapes value
        """
        await asyncio.sleep(0)
        return {"value": response.text}

    def tagged(self, response: TextResponse) -> None:
        """Use a custom request type.

        @url data:text/plain,tagged
        @tagged
        """


CONTRACTS = (
    UrlContract,
    CallbackKeywordArgumentsContract,
    MetadataContract,
    MethodContract,
    BodyContract,
    HeaderContract,
    CookieContract,
    ReturnsContract,
    ScrapesContract,
    TaggedRequestContract,
)


def _response(request: Request) -> TextResponse:
    return TextResponse(
        request.url,
        body=request.url.rsplit(",", 1)[-1].encode(),
        encoding="utf-8",
        request=request,
    )


async def main() -> None:
    spider = DemoSpider()
    manager = ContractsManager(CONTRACTS)

    configured_result = TestResult()
    configured = manager.from_method(spider.configured, configured_result)
    assert configured is not None and configured.callback is not None
    configured_output = configured.callback(
        _response(configured),
        **configured.cb_kwargs,
    )

    failing_result = TestResult()
    failing = manager.from_method(spider.failing, failing_result)
    assert failing is not None and failing.callback is not None
    failing.callback(_response(failing))

    async_result = TestResult()
    asynchronous = manager.from_method(spider.asynchronous, async_result)
    assert asynchronous is not None and asynchronous.callback is not None
    await asynchronous.callback(_response(asynchronous))

    tagged_result = TestResult()
    tagged = manager.from_method(spider.tagged, tagged_result)
    assert isinstance(tagged, TaggedRequest)

    settings = Settings()
    defaults = settings.get_component_priority_dict_with_base("SPIDER_CONTRACTS")
    print(
        json.dumps(
            {
                "async": {
                    "errors": len(async_result.errors),
                    "failures": len(async_result.failures),
                    "tests": async_result.testsRun,
                },
                "configured": {
                    "body": configured.body.decode(),
                    "callback_output": configured_output,
                    "cb_kwargs": configured.cb_kwargs,
                    "cookies": configured.cookies,
                    "errors": len(configured_result.errors),
                    "failures": len(configured_result.failures),
                    "header": configured.headers["X-Test"].decode(),
                    "meta": configured.meta,
                    "method": configured.method,
                    "tests": configured_result.testsRun,
                },
                "defaults": [str(reference) for reference in defaults],
                "discovery": manager.tested_methods_from_spidercls(DemoSpider),
                "failing": {
                    "errors": len(failing_result.errors),
                    "failures": len(failing_result.failures),
                    "message": failing_result.failures[-1][1].splitlines()[-1].split(": ", 1)[-1],
                    "tests": failing_result.testsRun,
                },
                "signatures": {
                    "Contract": list(inspect.signature(Contract).parameters),
                    "ContractsManager": list(inspect.signature(ContractsManager).parameters),
                },
                "tagged": {
                    "contract_tag": tagged.contract_tag,
                    "type": type(tagged).__name__,
                    "url": tagged.url,
                },
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
