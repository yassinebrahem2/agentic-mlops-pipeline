"""Proposal approval observer (I13, I19): attaches approvals and handles decisions and superseding."""

from __future__ import annotations

import logging

from mlops_agents.agents.approvals.contract import ApprovalObserver, Approvals, ApprovalView
from mlops_agents.proposals.service import ProposalsService
from mlops_agents.workspace.service import WorkspaceService

logger = logging.getLogger(__name__)


class ProposalApprovalObserver(ApprovalObserver):
    """Watches approval transitions for proposal tools.

    - asked: attaches the approval to the proposal draft and cancels any superseded approval.
    - decided: updates proposal status (approved -> apply, others -> reject/sweep).
    """

    def __init__(
        self,
        proposals: ProposalsService,
        approvals: Approvals,
        workspace: WorkspaceService,
    ) -> None:
        self._proposals = proposals
        self._approvals = approvals
        self._workspace = workspace
        self._approval_to_project: dict[str, str] = {}

    async def asked(self, view: ApprovalView) -> None:
        if view.proposal_kind is None or view.tool_call_id is None:
            return

        project_id = self._approval_to_project.get(view.id)
        draft = None
        if project_id is not None:
            draft = await self._proposals.for_call(project_id, view.turn_id, view.tool_call_id)
        else:
            for project in await self._workspace.list_projects():
                draft = await self._proposals.for_call(project.id, view.turn_id, view.tool_call_id)
                if draft is not None:
                    project_id = project.id
                    self._approval_to_project[view.id] = project_id
                    break

        if project_id is None or draft is None:
            return

        superseded_approval_id = await self._proposals.attach(project_id, draft.id, view.id)
        if superseded_approval_id is not None:
            self._approval_to_project[superseded_approval_id] = project_id
            await self._approvals.cancel(project_id, superseded_approval_id)

    async def decided(self, view: ApprovalView) -> None:
        if view.proposal_kind is None:
            return

        project_id = self._approval_to_project.get(view.id)
        proposal = None
        if project_id is not None:
            proposal = await self._proposals.get_by_approval(project_id, view.id)
        else:
            for project in await self._workspace.list_projects():
                proposal = await self._proposals.get_by_approval(project.id, view.id)
                if proposal is not None:
                    project_id = project.id
                    self._approval_to_project[view.id] = project_id
                    break

        if project_id is None or proposal is None:
            return

        await self._proposals.decide(project_id, proposal.id, approved=(view.status == "approved"))
