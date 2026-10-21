import httpx2
import openai

from mlops_agents.providers.errors import ProviderErrorKind, classify

REQUEST = httpx2.Request("POST", "https://x.io/v1/chat/completions")


def _response(status: int, headers: dict[str, str] | None = None) -> httpx2.Response:
    return httpx2.Response(status, request=REQUEST, headers=headers)


def test_auth() -> None:
    err = openai.AuthenticationError("bad key", response=_response(401), body=None)
    assert classify(err).kind is ProviderErrorKind.AUTH


def test_rate_limit_with_retry_after() -> None:
    err = openai.RateLimitError(
        "slow down", response=_response(429, {"retry-after": "3"}), body=None
    )
    result = classify(err)
    assert result.kind is ProviderErrorKind.RATE_LIMIT
    assert result.retry_after == 3.0


def test_quota() -> None:
    err = openai.RateLimitError(
        "You exceeded your current quota",
        response=_response(429),
        body={"code": "insufficient_quota"},
    )
    assert classify(err).kind is ProviderErrorKind.QUOTA


def test_overloaded_is_transient() -> None:
    err = openai.InternalServerError("high demand", response=_response(503), body=None)
    assert classify(err).kind is ProviderErrorKind.TRANSIENT


def test_connection_error_is_unreachable() -> None:
    assert (
        classify(openai.APIConnectionError(request=REQUEST)).kind is ProviderErrorKind.UNREACHABLE
    )


def test_wrapped_error_is_found_through_the_cause_chain() -> None:
    inner = openai.AuthenticationError("bad key", response=_response(401), body=None)
    try:
        try:
            raise inner
        except openai.AuthenticationError as exc:
            raise RuntimeError("wrapper") from exc
    except RuntimeError as outer:
        assert classify(outer).kind is ProviderErrorKind.AUTH


def test_message_hides_secrets() -> None:
    err = RuntimeError("failed with key gsk_abcdef123456")
    result = classify(err, ["gsk_abcdef123456"])
    assert result.kind is ProviderErrorKind.UNKNOWN
    assert "gsk_abcdef123456" not in result.message


def test_dropped_stream_is_unreachable() -> None:
    err = httpx2.RemoteProtocolError("peer closed connection without sending complete message body")
    assert classify(err).kind is ProviderErrorKind.UNREACHABLE
