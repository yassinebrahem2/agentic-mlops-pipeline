"""ProposalKind implementation for parser proposals (I19, I22)."""

from __future__ import annotations

import logging

from pydantic import BaseModel

from mlops_agents.agents.tools.base import Preview
from mlops_agents.monitoring.results.constants import HELD_TAIL_QUIET_SECONDS
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

# What a code parser's script is run with, shown on its card so the script is written against
# it. code_parser.py's bootstrap implements it.
CODE_PARSER_CONTRACT = (
    "The script runs once per new chunk of the file, in the project's sandbox, without network.\n"
    "- stdin: exactly the chunk, UTF-8 text, in whole lines: the file from its start on the"
    " first read (and after the file shrinks, truncated or replaced by a shorter one), then"
    " only the bytes past the previous read. A chunk ends at a newline, except a final line"
    " without one, given only once the file has been unchanged for"
    f" {HELD_TAIL_QUIET_SECONDS} s; the script judges whether it is complete.\n"
    "- sys.argv[1]: the file's path in the connection.\n"
    "- sys.argv[2]: the chunk's start byte offset in the file.\n"
    "- sys.argv[3]: the file's first line (a CSV header, say) when the start offset is above 0;"
    ' otherwise "", and the chunk itself begins with that line.\n'
    "- stdout: the records, one JSON object per line or one JSON array, and nothing else.\n"
    "- stderr is ignored. The script must not read the file itself."
)


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
            body = (
                f"Source: {source_id}\n"
                f"Timeout: {c.timeout_s}s\n"
                f"Script contract:\n{CODE_PARSER_CONTRACT}\n"
                f"Script:\n{c.script}"
            )
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
