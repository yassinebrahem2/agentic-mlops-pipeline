import httpx2
import pytest

from mlops_agents.providers.errors import ProviderCallError, ProviderErrorKind
from mlops_agents.providers.verification import probe_model
from tests.fakes.fake_openai import RunningFake


async def test_tool_calling_model_passes(
    fake_openai: RunningFake, model_http: httpx2.AsyncClient
) -> None:
    result = await probe_model(fake_openai.base_url, None, "fake-tools", http=model_http)
    assert result.tool_calling
    assert result.streaming
    assert result.detail is None
    sent = fake_openai.state.requests[-1]
    assert sent["stream"] is True
    assert sent["tools"][0]["function"]["name"] == "report_capabilities"


async def test_model_that_ignores_tools_is_reported(
    fake_openai: RunningFake, model_http: httpx2.AsyncClient
) -> None:
    result = await probe_model(fake_openai.base_url, None, "fake-no-tools", http=model_http)
    assert not result.tool_calling
    assert result.detail is not None
    assert "tool" in result.detail


async def test_missing_usage_in_stream_still_passes(
    fake_openai: RunningFake, model_http: httpx2.AsyncClient
) -> None:
    fake_openai.state.usage_in_stream = False
    result = await probe_model(fake_openai.base_url, None, "fake-tools", http=model_http)
    assert result.tool_calling


async def test_bad_key_is_an_auth_error(
    fake_openai: RunningFake, model_http: httpx2.AsyncClient
) -> None:
    fake_openai.state.required_key = "right"
    with pytest.raises(ProviderCallError) as err:
        await probe_model(fake_openai.base_url, "wrong-key-value", "fake-tools", http=model_http)
    assert err.value.kind is ProviderErrorKind.AUTH
    assert "wrong-key-value" not in err.value.message
