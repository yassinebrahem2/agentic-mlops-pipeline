from __future__ import annotations

from typing import TYPE_CHECKING, Any

import httpx

if TYPE_CHECKING:
    from fake_discord import FakeDiscordState, RunningFakeDiscord, start_fake_discord
else:
    from tests.fakes.fake_discord import FakeDiscordState, RunningFakeDiscord, start_fake_discord


def test_fake_discord_records_post_and_handles_failures() -> None:
    state = FakeDiscordState()
    server: RunningFakeDiscord = start_fake_discord(state)
    try:
        url = f"{server.base_url}/api/webhooks/123/token_abc"
        payload: dict[str, Any] = {"content": "Hello Discord", "allowed_mentions": {"parse": []}}

        # Successful post (204 No Content)
        resp = httpx.post(url, json=payload, timeout=5)
        assert resp.status_code == 204
        assert len(state.posts) == 1
        assert state.posts[0]["content"] == "Hello Discord"
        assert state.posts[0]["allowed_mentions"] == {"parse": []}
        assert state.posts[0]["path"] == "/api/webhooks/123/token_abc"

        # Scripted 429 failure
        state.fail_next(429)
        resp_429 = httpx.post(url, json=payload, timeout=5)
        assert resp_429.status_code == 429

        # Scripted 500 failure
        state.fail_next(500)
        resp_500 = httpx.post(url, json=payload, timeout=5)
        assert resp_500.status_code == 500

        # Success resumes
        resp_ok = httpx.post(url, json={"content": "Second post"}, timeout=5)
        assert resp_ok.status_code == 204
        assert len(state.posts) == 2

        # Reset
        state.reset()
        assert len(state.posts) == 0
    finally:
        server.stop()
