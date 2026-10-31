"""ProposalKind implementation for parser proposals (I19, I22)."""

from __future__ import annotations

import logging

from pydantic import BaseModel

from mlops_agents.agents.tools.base import Preview
from mlops_agents.monitoring.results.contract import (
    ParserPayload,
    Parsers,
)
from mlops_agents.proposals.contract import (
    PARSER_KIND,
    ApplyResult,
    CredentialSlot,
    parser_key,
)

logger = logging.getLogger(__name__)


def _payload(payload: BaseModel) -> ParserPayload:
    if isinstance(payload, ParserPayload):
        return payload
    return ParserPayload.model_validate(payload)


class ParserKind:
    """ProposalKind implementation for 'parser' proposals."""

    name = PARSER_KIND
    payload_model: type[BaseModel] = ParserPayload

    def __init__(self, parsers: Parsers) -> None:
        self._parsers = parsers

    def key(self, payload: BaseModel) -> str:
        return parser_key(_payload(payload))

    def preview(self, payload: BaseModel) -> Preview:
        p = _payload(payload)
        source_id = p.spec.source_id
        if p.spec.mapping is not None:
            m = p.spec.mapping
            body = (
                f"Source: {source_id}\n"
                f"Format: {m.format}\n"
                f"Fields: {', '.join(f'{k}->{v}' for k, v in m.fields.items())}\n"
                f"Pattern: {m.pattern or 'None'}\n"
                f"Condition: {m.fired_when.model_dump_json()}"
            )
        elif p.spec.code is not None:
            c = p.spec.code
            body = f"Source: {source_id}\nTimeout: {c.timeout_s}s\nScript:\n{c.script}"
        else:
            body = f"Source: {source_id}"

        return Preview(kind="text", title=f"Parser for {source_id}", body=body)

    def warnings(self, payload: BaseModel) -> list[str]:
        return []

    def credentials_needed(self, payload: BaseModel) -> list[CredentialSlot]:
        return []

    async def apply(
        self,
        project_id: str,
        proposal_id: str,
        payload: BaseModel,
    ) -> ApplyResult:
        p = _payload(payload)
        source_id = p.spec.source_id
        await self._parsers.store(project_id, p.spec, proposal_id)
        return ApplyResult(applied=[f"parser:{source_id}"], failed={})
