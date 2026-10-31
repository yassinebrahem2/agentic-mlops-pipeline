from __future__ import annotations

import smtplib
from email.message import EmailMessage
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from fake_smtp import FakeSmtpState, RunningFakeSmtp, start_fake_smtp
else:
    from tests.fakes.fake_smtp import FakeSmtpState, RunningFakeSmtp, start_fake_smtp


def test_fake_smtp_receives_message() -> None:
    state = FakeSmtpState()
    server: RunningFakeSmtp = start_fake_smtp(state)
    try:
        msg = EmailMessage()
        msg["Subject"] = "Test Alert"
        msg["From"] = "alerts@example.com"
        msg["To"] = "oncall@example.com"
        msg.set_content("Everything is broken!")

        with smtplib.SMTP(server.host, server.port, timeout=5) as client:
            client.ehlo()
            client.send_message(msg)

        assert len(state.messages) == 1
        rec = state.messages[0]
        assert rec.from_addr == "alerts@example.com"
        assert rec.to_addrs == ["oncall@example.com"]
        assert "Everything is broken!" in rec.raw_data
    finally:
        server.stop()


def test_fake_smtp_auth_login_and_plain() -> None:
    state = FakeSmtpState()
    server: RunningFakeSmtp = start_fake_smtp(state)
    try:
        # Test AUTH LOGIN
        with smtplib.SMTP(server.host, server.port, timeout=5) as client:
            client.ehlo()
            code, _resp = client.login("user", "pass")
            assert code == 235

        # Test AUTH PLAIN
        with smtplib.SMTP(server.host, server.port, timeout=5) as client:
            client.ehlo()
            client.user = "user"
            client.password = "pass"
            code, _resp = client.auth("PLAIN", client.auth_plain, initial_response_ok=True)
            assert code == 235
    finally:
        server.stop()


def test_fake_smtp_starttls_answers_454() -> None:
    state = FakeSmtpState()
    server: RunningFakeSmtp = start_fake_smtp(state)
    try:
        with smtplib.SMTP(server.host, server.port, timeout=5) as client:
            client.ehlo()
            with pytest.raises(smtplib.SMTPResponseException) as exc_info:
                client.starttls()
            assert exc_info.value.smtp_code == 454
    finally:
        server.stop()


def test_fake_smtp_fail_next_sends() -> None:
    state = FakeSmtpState()
    server: RunningFakeSmtp = start_fake_smtp(state)
    try:
        state.fail_next_sends = 1
        msg = EmailMessage()
        msg["Subject"] = "Test"
        msg["From"] = "sender@example.com"
        msg["To"] = "receiver@example.com"
        msg.set_content("Content")

        with smtplib.SMTP(server.host, server.port, timeout=5) as client:
            client.ehlo()
            with pytest.raises(smtplib.SMTPResponseException) as exc_info:
                client.send_message(msg)
            assert exc_info.value.smtp_code in (451, 450, 550)

        # Subsequent send succeeds
        with smtplib.SMTP(server.host, server.port, timeout=5) as client:
            client.ehlo()
            client.send_message(msg)
        assert len(state.messages) == 1
    finally:
        server.stop()
