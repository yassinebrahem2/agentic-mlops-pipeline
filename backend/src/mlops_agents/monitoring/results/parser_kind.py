"""ProposalKind implementation for parser proposals (I19, I22)."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any, Protocol

from pydantic import BaseModel

from mlops_agents.agents.tools.base import Preview
from mlops_agents.monitoring.results.constants import (
    CODE_PARSER_CHUNK_BYTES,
    CODE_PARSER_MAX_CHUNKS_PER_CYCLE,
    HELD_TAIL_QUIET_SECONDS,
)
from mlops_agents.monitoring.results.contract import (
    ParserPayload,
    Parsers,
    SourceKind,
)
from mlops_agents.monitoring.results.mapping import evaluate_mapping
from mlops_agents.proposals.contract import (
    PARSER_KIND,
    ApplyResult,
    CredentialSlot,
    ProposalCheck,
    parser_key,
)
from mlops_agents.sandbox.docker import HEAD_CAPTURE_CHARS, TAIL_CAPTURE_CHARS

logger = logging.getLogger(__name__)

# What a code parser's script is run with, shown on its card so the script is written against
# it. code_parser.py's bootstrap implements it.
CODE_PARSER_CONTRACT = (
    "The script runs once per chunk of the file, each chunk a separate run in the project's"
    " sandbox, without network. A chunk is at most"
    f" {CODE_PARSER_CHUNK_BYTES / 1024:g} KiB and starts at a line start; a single line longer"
    " than that is a chunk of its own. A chunk whose output (stdout and stderr together)"
    f" exceeds {HEAD_CAPTURE_CHARS + TAIL_CAPTURE_CHARS:,} characters is run again in smaller"
    " pieces, each starting at a line start, each a separate run with its own start offset."
    " The script must not assume it sees the whole file. A collection cycle runs at most"
    f" {CODE_PARSER_MAX_CHUNKS_PER_CYCLE} chunks of a file; the rest are read on later cycles,"
    " in order.\n"
    "- stdin: exactly the chunk, UTF-8 text, in whole lines. The script gets whole lines and"
    " nothing joins a record that spans a line boundary for it, so a format whose records can"
    " span lines (a csv with quoted newlines, say) belongs to a mapping parser. The chunks"
    " cover the file from its start on the first read (and after the file shrinks, truncated"
    " or replaced by a shorter one), then go on from where the previous chunk ended. A chunk"
    " ends at a newline, except the"
    " file's final line without one, given only once the file has been unchanged for"
    f" {HELD_TAIL_QUIET_SECONDS} s; the script judges whether it is complete.\n"
    "- sys.argv[1]: the file's path in the connection.\n"
    "- sys.argv[2]: the chunk's start byte offset in the file.\n"
    "- sys.argv[3]: the file's first line (a CSV header, say) when the start offset is above 0;"
    ' otherwise "", and the chunk itself begins with that line.\n'
    "- stdout: the records, one JSON object per line or one JSON array, and nothing else.\n"
    "- stderr is ignored. The script must not read the file itself."
)


class ResultsSourceProvider(Protocol):
    async def sources(self, project_id: str) -> list[tuple[str, SourceKind, bool]]: ...


def _payload(payload: Any) -> ParserPayload:
    if isinstance(payload, ParserPayload):
        return payload
    return ParserPayload.model_validate(payload)


def _normalize_datetime(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _ran_at_matches(dt1: datetime, dt2: datetime) -> bool:
    n1 = _normalize_datetime(dt1)
    n2 = _normalize_datetime(dt2)
    if n1 == n2:
        return True
    return n1.replace(second=0, microsecond=0) == n2.replace(second=0, microsecond=0)


class ParserKind(ProposalCheck):
    """ProposalKind implementation for 'parser' proposals."""

    name = PARSER_KIND
    payload_model: type[BaseModel] = ParserPayload

    def __init__(self, parsers: Parsers, results: ResultsSourceProvider | None = None) -> None:
        self._parsers = parsers
        self._results = results

    async def check(self, project_id: str, payload: Any) -> list[str]:
        """Refusals returned at propose time before the gate."""
        p = _payload(payload)
        refusals: list[str] = []

        if self._results is not None:
            sources = await self._results.sources(project_id)
            needs_parser_ids = [s_id for s_id, _kind, needs in sources if needs]
            if p.spec.source_id not in needs_parser_ids:
                if needs_parser_ids:
                    refusals.append(
                        f"Propose the parser for one of these source ids, exactly as written: {', '.join(needs_parser_ids)}"
                    )
                else:
                    refusals.append("This project has no source that needs a parser.")

        if p.spec.mapping is not None:
            m = p.spec.mapping
            if "ran_at" in m.defaults:
                refusals.append(
                    "Mapping parser cannot set 'ran_at' in defaults; records need the time the check ran, mapped from a field."
                )
            if "ran_at" not in m.fields:
                refusals.append(
                    "Mapping parser must map 'ran_at' from a field; records need the time the check ran, mapped from a field."
                )
            if not refusals:
                outcome = evaluate_mapping(m, p.sample_input, origin_ref="sample:mapping")
                if outcome.errors:
                    refusals.append(
                        f"sample_records do not match what the parser produces from sample_input: {outcome.errors[0]}"
                    )
                elif len(outcome.records) != len(p.sample_records):
                    produced = [r.model_dump(mode="json") for _, r in outcome.records]
                    refusals.append(
                        f"sample_records do not match what the parser produces from sample_input: "
                        f"expected {len(p.sample_records)} record(s), got {len(outcome.records)}: {produced}"
                    )
                else:
                    for idx, (exp, (_, got)) in enumerate(
                        zip(p.sample_records, outcome.records, strict=True)
                    ):
                        if exp.check_id != got.check_id:
                            refusals.append(
                                f"sample_records do not match what the parser produces from sample_input: "
                                f"record {idx} check_id: expected {exp.check_id!r}, got {got.check_id!r}"
                            )
                            break
                        if exp.category != got.category:
                            refusals.append(
                                f"sample_records do not match what the parser produces from sample_input: "
                                f"record {idx} category: expected {exp.category!r}, got {got.category!r}"
                            )
                            break
                        if exp.fired != got.fired:
                            refusals.append(
                                f"sample_records do not match what the parser produces from sample_input: "
                                f"record {idx} fired: expected {exp.fired!r}, got {got.fired!r}"
                            )
                            break
                        if exp.value != got.value:
                            refusals.append(
                                f"sample_records do not match what the parser produces from sample_input: "
                                f"record {idx} value: expected {exp.value!r}, got {got.value!r}"
                            )
                            break
                        if exp.threshold != got.threshold:
                            refusals.append(
                                f"sample_records do not match what the parser produces from sample_input: "
                                f"record {idx} threshold: expected {exp.threshold!r}, got {got.threshold!r}"
                            )
                            break
                        if exp.subject != got.subject:
                            refusals.append(
                                f"sample_records do not match what the parser produces from sample_input: "
                                f"record {idx} subject: expected {exp.subject!r}, got {got.subject!r}"
                            )
                            break
                        if exp.model != got.model:
                            refusals.append(
                                f"sample_records do not match what the parser produces from sample_input: "
                                f"record {idx} model: expected {exp.model!r}, got {got.model!r}"
                            )
                            break
                        if not _ran_at_matches(exp.ran_at, got.ran_at):
                            refusals.append(
                                f"sample_records do not match what the parser produces from sample_input: "
                                f"record {idx} ran_at: expected {exp.ran_at.isoformat()}, got {got.ran_at.isoformat()}"
                            )
                            break
        return refusals

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
                "The sample records were written by the agent; the app did not run this parser on the sample.\n"
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
