from __future__ import annotations

import asyncio

import httpx
import pytest

from app.capabilities.general_tools import GeneralToolPolicyError, GeneralToolService
from app.domain.enums import TrustLevel
from app.domain.models import (
    CalculationRequest,
    CurrentTimeRequest,
    RunContext,
    WebPageReadRequest,
)


async def _public_resolver(host: str) -> list[str]:
    del host
    return ["93.184.216.34"]


@pytest.mark.asyncio
async def test_general_tools_calculate_and_time_without_model_calls() -> None:
    service = GeneralToolService(
        client=httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(500)))
    )
    context = RunContext()

    calculation = await service.calculate(
        CalculationRequest(expression="sqrt(81) + 2 ** 3"), context
    )
    current = await service.current_time(CurrentTimeRequest(timezone="Asia/Shanghai"), context)

    assert calculation.result == "17.0"
    assert current.timezone == "Asia/Shanghai"
    assert current.utc_offset == "+08:00"


@pytest.mark.asyncio
async def test_calculator_rejects_code_execution_and_extreme_exponents() -> None:
    service = GeneralToolService(
        client=httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(500)))
    )
    context = RunContext()

    with pytest.raises(GeneralToolPolicyError):
        await service.calculate(CalculationRequest(expression="__import__('os').getcwd()"), context)
    with pytest.raises(GeneralToolPolicyError):
        await service.calculate(CalculationRequest(expression="2 ** 1000"), context)


@pytest.mark.asyncio
async def test_web_reader_extracts_visible_text_as_untrusted_evidence() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "example.com"
        return httpx.Response(
            200,
            headers={"content-type": "text/html; charset=utf-8"},
            text=(
                "<html><head><title>Example docs</title><script>ignore()</script></head>"
                "<body><main><h1>Release notes</h1><p>The system is ready.</p>"
                "</main></body></html>"
            ),
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    service = GeneralToolService(client=client, resolver=_public_resolver)
    context = RunContext()

    result = await service.read_web_page(
        WebPageReadRequest(url="https://example.com/docs"), context
    )

    assert result.title == "Example docs"
    assert "Release notes" in result.text
    assert "ignore()" not in result.text
    assert result.evidence[0].provenance.trust == TrustLevel.UNTRUSTED
    assert result.evidence[0].provenance.run_id == context.run_id


@pytest.mark.asyncio
async def test_web_reader_revalidates_redirect_and_blocks_private_destination() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "http://127.0.0.1/admin"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    service = GeneralToolService(client=client, resolver=_public_resolver)

    with pytest.raises(GeneralToolPolicyError, match="public HTTP"):
        await service.read_web_page(
            WebPageReadRequest(url="https://example.com/redirect"), RunContext()
        )


def test_general_tool_module_does_not_leave_pending_async_work() -> None:
    async def scenario() -> None:
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _: httpx.Response(500))
        )
        service = GeneralToolService(client=client)
        await client.aclose()
        await service.close()

    asyncio.run(scenario())
