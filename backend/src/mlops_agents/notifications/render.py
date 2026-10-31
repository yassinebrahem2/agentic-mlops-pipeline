"""The one renderer of a Message into plain text for Discord and Email channels."""

from __future__ import annotations

from mlops_agents.notifications.contract import Message


def render_message(message: Message) -> str:
    """Turns a Message into the plain text both channels send."""
    sections: list[str] = [
        message.title,
        f"Severity: {message.severity}",
    ]

    if message.cause:
        sections.append(f"Cause: {message.cause}")
    elif message.diagnosis_pending:
        sections.append("Cause: diagnosis follows")

    if message.done:
        done_text = "\n".join(f"- {item}" for item in message.done)
        sections.append(f"Done:\n{done_text}")

    if message.waiting:
        waiting_text = "\n".join(f"- {item}" for item in message.waiting)
        sections.append(f"Waiting:\n{waiting_text}")

    if message.lines:
        sections.append("\n".join(message.lines))

    if message.link:
        sections.append(f"Link: {message.link}")

    if message.local_mode_note:
        sections.append(
            "Note: The app is running in local mode; this link opens only on the app's machine."
        )

    return "\n\n".join(sections)


render = render_message
