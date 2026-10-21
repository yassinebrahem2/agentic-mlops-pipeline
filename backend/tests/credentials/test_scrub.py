import httpx2
import pytest

from mlops_agents.credentials.scrub import (
    base_url_problem,
    error_text,
    mask_key,
    scrub_url_secrets,
    scrub_values,
)


@pytest.mark.parametrize(
    "url",
    [
        "https://api.groq.com/openai/v1",
        "http://localhost:11434/v1",
        "https://x.io/v1?api-version=2",
    ],
)
def test_valid_base_urls(url: str) -> None:
    assert base_url_problem(url) is None


@pytest.mark.parametrize(
    ("url", "reason"),
    [
        ("ftp://x.io", "http:// or https://"),
        ("https://user:pw@x.io", "must not carry credentials"),
        ("https://x.io/v1?api_key=abc", "parameter 'api_key'"),
        ("https://x.io/v1?API-KEY=abc", "parameter 'api_key'"),
        ("https://x.io/v1 ", "whitespace"),
        ("https:///v1", "no host"),
    ],
)
def test_invalid_base_urls(url: str, reason: str) -> None:
    problem = base_url_problem(url)
    assert problem is not None
    assert reason in problem
    assert "abc" not in problem


def test_scrub_url_secrets_masks_values() -> None:
    text = "failed: https://u:pw@x.io/v1?token=abc&v=1"
    assert scrub_url_secrets(text) == "failed: https://***@x.io/v1?token=***&v=***"


def test_scrub_values_masks_each_secret() -> None:
    assert scrub_values("key sk-123 sent", ["sk-123"]) == "key *** sent"
    assert scrub_values("nothing here", ["", "abc"]) == "nothing here"


def test_mask_key() -> None:
    assert mask_key("gsk_abcdefgh1234") == "••••1234"
    assert mask_key("short") == "••••"


def test_error_text_withholds_url_parse_errors() -> None:
    try:
        httpx2.Request("GET", "http://host:gsk_secret_value/v1")
    except Exception as exc:
        assert "gsk_secret_value" not in error_text(exc)
        assert "withheld" in error_text(exc)
    else:
        pytest.fail("the URL should not parse")
