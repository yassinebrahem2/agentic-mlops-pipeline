"""SendMessage's behavior (I12): who may be messaged, which exchange and hop, the limits."""

from __future__ import annotations

from sqlalchemy import delete, func, insert, select
from sqlalchemy.ext.asyncio import AsyncConnection

from mlops_agents.agents.catalog import AgentCatalog
from mlops_agents.agents.messaging.contract import MessageBus, MessageLimitReached
from mlops_agents.agents.messaging.settings import MessagingSettings
from mlops_agents.agents.runtime.contract import AgentSessions
from mlops_agents.clock import now_iso
from mlops_agents.errors import DomainError
from mlops_agents.events.contract import EventLog
from mlops_agents.ids import new_id
from mlops_agents.storage.databases import Databases
from mlops_agents.storage.tables import exchanges, llm_calls, messages


class SqliteMessageBus(MessageBus):
    """The bus is built before the catalog (the catalog needs the tool names, one of which is
    SendMessage) and before the session manager, so both arrive later: `use_catalog` and `bind`."""

    def __init__(self, databases: Databases, events: EventLog, settings: MessagingSettings) -> None:
        self._databases = databases
        self._events = events
        self._settings = settings
        self._catalog: AgentCatalog | None = None
        self._sessions: AgentSessions | None = None

    def use_catalog(self, catalog: AgentCatalog) -> None:
        if self._catalog is not None:
            raise RuntimeError("The message bus already has its catalog.")
        self._catalog = catalog

    def bind(self, sessions: AgentSessions) -> None:
        if self._sessions is not None:
            raise RuntimeError("The message bus is already bound.")
        self._sessions = sessions

    def display_name(self, agent: str) -> str:
        return self._require_catalog().get(agent).display_name

    async def send(
        self, project_id: str, *, sender: str, sender_turn_id: str, to: str, text: str
    ) -> None:
        catalog = self._require_catalog()
        sessions = self._sessions
        if sessions is None:
            raise RuntimeError("The message bus is not bound to the session manager.")
        sender_definition = catalog.get(sender)
        recipient = catalog.get(to)  # an unknown agent is a DomainError
        if to not in sender_definition.can_message:
            raise DomainError(
                "message_not_allowed",
                f"{sender_definition.display_name} may not message {recipient.display_name}.",
                status=403,
            )

        limits = await self._settings.get(project_id)
        engine = await self._databases.project(project_id)
        async with engine.connect() as conn:
            read = (
                (
                    await conn.execute(
                        select(messages.c.exchange_id, messages.c.hop)
                        .where(
                            messages.c.read_by_turn == sender_turn_id,
                            messages.c.from_agent == to,
                            messages.c.exchange_id.is_not(None),
                            messages.c.hop.is_not(None),
                        )
                        .order_by(messages.c.seq.desc())
                        .limit(1)
                    )
                )
                .mappings()
                .first()
            )
            exchange_id = None if read is None else str(read["exchange_id"])
            hop = 1 if read is None else int(read["hop"]) + 1
            if hop > limits.max_hops:
                raise MessageLimitReached(
                    f"This exchange has reached its limit of {limits.max_hops} messages. "
                    "Do not send another message; report to the user instead."
                )
            if exchange_id is not None:
                used = await self._tokens_used(conn, exchange_id)
                if used > limits.max_tokens:
                    raise MessageLimitReached(
                        f"This exchange has used {used} tokens, past its limit of "
                        f"{limits.max_tokens}. Do not send another message; "
                        "report to the user instead."
                    )

        created = exchange_id is None
        if exchange_id is None:
            exchange_id = new_id("xch")
            async with engine.begin() as conn:
                await conn.execute(insert(exchanges).values(id=exchange_id, created_at=now_iso()))

        try:
            posted = await sessions.post(
                project_id,
                to,
                author_name=sender_definition.display_name,
                from_agent=sender,
                text=text,
                exchange_id=exchange_id,
                hop=hop,
            )
        except DomainError:
            # post refuses an empty text or a recipient that cannot chat: no message holds the
            # new exchange, so it is not left behind.
            if created:
                async with engine.begin() as conn:
                    await conn.execute(delete(exchanges).where(exchanges.c.id == exchange_id))
            raise
        await self._events.record(
            project_id,
            source=sender,
            type="message.sent",
            subject_id=posted.id,
            payload={"sender": sender, "recipient": to, "exchange_id": exchange_id, "hop": hop},
        )

    def _require_catalog(self) -> AgentCatalog:
        if self._catalog is None:
            raise RuntimeError("The message bus has no catalog yet.")
        return self._catalog

    @staticmethod
    async def _tokens_used(conn: AsyncConnection, exchange_id: str) -> int:
        """Tokens of the llm_calls of the turns that read the exchange's messages."""
        readers = (
            select(messages.c.read_by_turn)
            .where(messages.c.exchange_id == exchange_id, messages.c.read_by_turn.is_not(None))
            .distinct()
        )
        total = await conn.scalar(
            select(
                func.coalesce(
                    func.sum(
                        func.coalesce(llm_calls.c.prompt_tokens, 0)
                        + func.coalesce(llm_calls.c.completion_tokens, 0)
                    ),
                    0,
                )
            ).where(llm_calls.c.turn_id.in_(readers))
        )
        return int(total or 0)
