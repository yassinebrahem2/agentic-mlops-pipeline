"""Training constants."""

from __future__ import annotations

from typing import Final

REQUEST_TAG: Final = "mlops_agents.request_id"
RETRAINING_AGENT: Final = "retraining"

TRAINING_REQUEST_DEADLINE_MINUTES: Final = 1440
TRAINING_REQUEST_GRACE_MINUTES: Final = 30

EVIDENCE_MAX_CHARS: Final = 4000
WHY_MAX_CHARS: Final = 4000
WHY_NOT_MAX_CHARS: Final = 4000
