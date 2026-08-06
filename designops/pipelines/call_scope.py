"""Attendee-based external-call detection (ported from transcript-processor call-scope)."""

from __future__ import annotations

INTERNAL_EMAIL_DOMAINS = ("@scandiweb.com", "@scandipwa.com")


def is_internal_email(email: str | None) -> bool:
    if not email:
        return False
    lower = email.lower()
    return any(lower.endswith(d) for d in INTERNAL_EMAIL_DOMAINS)


def is_external_call(
    attendees: list[dict] | None,
    client_participants: list[str] | None,
) -> bool:
    """True when the call has at least one non-internal attendee (or stored client participants)."""
    if isinstance(client_participants, list) and len(client_participants) > 0:
        return True
    if not attendees or not isinstance(attendees, list):
        return False
    return any(
        a.get("email") and not is_internal_email(str(a["email"])) for a in attendees if isinstance(a, dict)
    )
