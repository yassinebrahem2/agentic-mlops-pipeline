"""Email notification channel implementation."""

from __future__ import annotations

import asyncio
import smtplib
import ssl
from email.message import EmailMessage
from typing import Literal

from mlops_agents.credentials.scrub import error_text, scrub_values
from mlops_agents.notifications.constants import ERROR_MAX_CHARS, SEND_TIMEOUT_S
from mlops_agents.notifications.contract import Channel, ChannelError, Message
from mlops_agents.notifications.render import render_message
from mlops_agents.notifications.settings import IntegrationSettings


class EmailChannel(Channel):
    name: Literal["discord", "email"] = "email"

    def __init__(self, settings: IntegrationSettings) -> None:
        self._settings = settings

    async def send(self, target: str, message: Message) -> None:
        email_settings = await self._settings.email()
        password = await self._settings.email_password()
        secret_values = await self._settings.secret_values()

        if not email_settings.host or not email_settings.sender:
            msg = "Email channel is not configured."
            raise ChannelError(scrub_values(msg, secret_values)[:ERROR_MAX_CHARS])

        # Strip CR and LF from the subject to prevent header injection
        subject = " ".join(message.title.replace("\r", " ").replace("\n", " ").split())
        body = render_message(message)

        target_addr = target.strip()
        sender_addr = email_settings.sender.strip()
        address_secrets = [*list(secret_values), target_addr, sender_addr]

        em = EmailMessage()
        em["Subject"] = subject
        em["From"] = sender_addr
        em["To"] = target_addr
        if message.urgent:
            em["Importance"] = "high"
            em["X-Priority"] = "1"
        em.set_content(body)

        def _send_sync() -> None:
            use_ssl = email_settings.port == 465
            if password and not (use_ssl or email_settings.starttls):
                raise ChannelError(
                    "Cannot send email password over a plain connection without TLS (starttls is false)."
                )

            ssl_context = ssl.create_default_context()
            try:
                smtp = (
                    smtplib.SMTP_SSL(
                        email_settings.host,
                        email_settings.port,
                        timeout=SEND_TIMEOUT_S,
                        context=ssl_context,
                    )
                    if use_ssl
                    else smtplib.SMTP(
                        email_settings.host, email_settings.port, timeout=SEND_TIMEOUT_S
                    )
                )
                with smtp:
                    if not use_ssl and email_settings.starttls:
                        smtp.ehlo()
                        smtp.starttls(context=ssl_context)
                        smtp.ehlo()
                    if password:
                        smtp.login(email_settings.user, password)
                    smtp.send_message(em)
            except ChannelError:
                raise
            except smtplib.SMTPRecipientsRefused:
                raise ChannelError("SMTP recipient address refused by server") from None
            except smtplib.SMTPSenderRefused:
                raise ChannelError("SMTP sender address refused by server") from None
            except Exception as exc:
                scrubbed = scrub_values(error_text(exc), address_secrets)[:ERROR_MAX_CHARS]
                raise ChannelError(scrubbed) from None

        try:
            await asyncio.to_thread(_send_sync)
        except ChannelError:
            raise
        except Exception as exc:
            scrubbed = scrub_values(error_text(exc), address_secrets)[:ERROR_MAX_CHARS]
            raise ChannelError(scrubbed) from None
