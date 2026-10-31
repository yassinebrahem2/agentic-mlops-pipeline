from __future__ import annotations

from mlops_agents.notifications.contract import Message
from mlops_agents.notifications.render import render_message


def test_render_message_all_fields() -> None:
    msg = Message(
        title="Model drift detected",
        severity="critical",
        cause="KS test failed on column age",
        done=["Rolled back deployment", "Notified on-call"],
        waiting=["Human approval for retrain"],
        lines=["Metric drift: p=0.001", "Sample size: 5000"],
        link="https://app.example.com/projects/prj_1/incidents/inc_1",
        local_mode_note=True,
        urgent=True,
        diagnosis_pending=False,
    )
    rendered = render_message(msg)

    assert "Model drift detected" in rendered
    assert "Severity: critical" in rendered
    assert "Cause: KS test failed on column age" in rendered
    assert "Done:" in rendered
    assert "- Rolled back deployment" in rendered
    assert "Waiting:" in rendered
    assert "- Human approval for retrain" in rendered
    assert "Metric drift: p=0.001" in rendered
    assert "Link: https://app.example.com/projects/prj_1/incidents/inc_1" in rendered
    assert (
        "Note: The app is running in local mode; this link opens only on the app's machine."
        in rendered
    )


def test_render_message_diagnosis_pending() -> None:
    msg = Message(
        title="Accuracy dropped",
        severity="high",
        cause=None,
        diagnosis_pending=True,
    )
    rendered = render_message(msg)
    assert "Accuracy dropped" in rendered
    assert "Severity: high" in rendered
    assert "Cause: diagnosis follows" in rendered
    assert "Link:" not in rendered
    assert "Note:" not in rendered
