from __future__ import annotations

from contextlib import AsyncExitStack
from pathlib import Path

from mlops_agents.api.services import build_services
from mlops_agents.config import RunMode, load_settings
from mlops_agents.credentials.keystore import KeyStore
from mlops_agents.events.audit import SqliteAuditLog
from mlops_agents.monitoring.jobs import JobKind
from mlops_agents.notifications.notifier import SqliteNotifier
from mlops_agents.notifications.settings import IntegrationSettings
from mlops_agents.paths import DataPaths
from mlops_agents.storage.databases import Databases
from tests.sandbox.fakes import FakeDockerRunner


class MemoryKeyStore(KeyStore):
    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    def get(self, ref: str) -> str | None:
        return self.store.get(ref)

    def set(self, ref: str, value: str) -> None:
        self.store[ref] = value

    def delete(self, ref: str) -> None:
        self.store.pop(ref, None)


async def test_app_secrets_includes_integration_secrets(tmp_path: Path) -> None:
    paths = DataPaths(tmp_path)
    paths.ensure()
    databases = Databases(paths)
    try:
        keystore = MemoryKeyStore()
        settings = IntegrationSettings(databases, keystore)

        webhook_url = "https://discord.com/api/webhooks/123/my_webhook_secret_token"
        await settings.set_discord(enabled=True, webhook_url=webhook_url)
        await settings.set_email(
            enabled=True,
            host="smtp.example.com",
            port=587,
            user="alerts",
            sender="noreply@example.com",
            starttls=True,
            password="email_super_secret_password",
        )

        secrets = await settings.secret_values()
        assert webhook_url in secrets
        assert "my_webhook_secret_token" in secrets
        assert "email_super_secret_password" in secrets
    finally:
        await databases.dispose()


async def test_build_services_registers_retry_notifications_job(tmp_path: Path) -> None:
    app_settings = load_settings(
        RunMode.LOCAL,
        env={"MLOPS_AGENTS_HOME": str(tmp_path)},
    )
    keystore = MemoryKeyStore()

    async with AsyncExitStack() as stack:
        services = await build_services(
            stack,
            app_settings,
            keystore=keystore,
            gh=None,
            docker_runner=FakeDockerRunner(),
        )

        assert (
            JobKind.RETRY_NOTIFICATIONS in services.scheduler._jobs  # pyright: ignore[reportPrivateUsage]
        )
        assert (
            JobKind.ESCALATE in services.scheduler._jobs  # pyright: ignore[reportPrivateUsage]
        )
        assert isinstance(services.notifier, SqliteNotifier)
        assert (
            services.notifier._incidents is services.incidents  # pyright: ignore[reportPrivateUsage]
        )

        # Configure webhook and password
        await services.integration_settings.set_discord(
            enabled=True,
            webhook_url="https://discord.com/api/webhooks/777/secret777",
        )
        await services.integration_settings.set_email(
            enabled=True,
            host="smtp.example.com",
            port=587,
            user="user",
            sender="a@b.com",
            starttls=True,
            password="email_secret_password_999",
        )

        # Audit scrubber test: the audit log must scrub webhook and password
        assert isinstance(services.audit, SqliteAuditLog)
        secrets = set(
            await services.audit._get_secrets()  # pyright: ignore[reportPrivateUsage]
        )
        assert "https://discord.com/api/webhooks/777/secret777" in secrets
        assert "secret777" in secrets
        assert "email_secret_password_999" in secrets
