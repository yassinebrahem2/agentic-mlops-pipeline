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


def test_a_429_worded_quota_exceeded_is_a_rate_limit() -> None:
    err = openai.RateLimitError(
        "Quota exceeded for metric: generate_content requests per minute",
        response=_response(429),
        body={"error": {"message": "quota exceeded", "status": "RESOURCE_EXHAUSTED"}},
    )
    assert classify(err).kind is ProviderErrorKind.RATE_LIMIT


def test_a_429_with_nested_insufficient_quota_is_quota() -> None:
    err = openai.RateLimitError(
        "out of credit",
        response=_response(429),
        body={"error": {"message": "out of credit", "code": "insufficient_quota"}},
    )
    assert classify(err).kind is ProviderErrorKind.QUOTA


def test_payment_required_is_quota() -> None:
    err = openai.APIStatusError("payment required", response=_response(402), body=None)
    assert classify(err).kind is ProviderErrorKind.QUOTA


def test_forbidden_is_auth() -> None:
    err = openai.PermissionDeniedError("forbidden", response=_response(403), body=None)
    assert classify(err).kind is ProviderErrorKind.AUTH


def test_server_error_is_transient() -> None:
    err = openai.InternalServerError("boom", response=_response(500), body=None)
    assert classify(err).kind is ProviderErrorKind.TRANSIENT


def test_retry_after_as_an_http_date_is_none() -> None:
    err = openai.RateLimitError(
        "slow down",
        response=_response(429, {"retry-after": "Wed, 28 Oct 2026 14:03:00 GMT"}),
        body=None,
    )
    result = classify(err)
    assert result.kind is ProviderErrorKind.RATE_LIMIT
    assert result.retry_after is None


def test_rate_limit_without_retry_after_is_none() -> None:
    err = openai.RateLimitError("slow down", response=_response(429), body=None)
    assert classify(err).retry_after is None


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


def test_bad_generation_tool_use_failed_top_level() -> None:
    err = openai.BadRequestError(
        "Tool call validation failed",
        response=_response(400),
        body={"code": "tool_use_failed"},
    )
    result = classify(err)
    assert result.kind is ProviderErrorKind.BAD_GENERATION


def test_bad_generation_tool_use_failed_nested() -> None:
    err = openai.BadRequestError(
        "Tool call validation failed",
        response=_response(400),
        body={"error": {"message": "failed", "code": "tool_use_failed"}},
    )
    result = classify(err)
    assert result.kind is ProviderErrorKind.BAD_GENERATION


def test_bad_generation_json_validate_failed() -> None:
    err = openai.BadRequestError(
        "JSON validation failed",
        response=_response(400),
        body={"error": {"code": "json_validate_failed"}},
    )
    result = classify(err)
    assert result.kind is ProviderErrorKind.BAD_GENERATION


def test_bad_generation_sse_api_error() -> None:
    err = openai.APIError(
        "Tool call validation failed during stream",
        request=REQUEST,
        body={"code": "tool_use_failed"},
    )
    result = classify(err)
    assert result.kind is ProviderErrorKind.BAD_GENERATION


def test_bad_request_other_code_is_bad_request() -> None:
    err = openai.BadRequestError(
        "Invalid model parameter",
        response=_response(400),
        body={"error": {"code": "invalid_parameter"}},
    )
    result = classify(err)
    assert result.kind is ProviderErrorKind.BAD_REQUEST
