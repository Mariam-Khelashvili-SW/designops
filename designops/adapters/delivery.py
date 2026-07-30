"""Delivery (§6.5, §12.4).

The go_live gate is enforced HERE, in the adapter — not just hidden in the UI. A
misconfigured send_mode during testing that mails a half-built digest to the head of
design is the single most expensive failure this project can have, and it costs one
`if` to prevent. In v1 every pipeline ships go_live=false, so `deliver()` always
returns blocked_go_live and nothing leaves the system.
"""

from __future__ import annotations

import re
import smtplib
from dataclasses import dataclass
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formatdate, make_msgid

from designops.core.config import Settings, get_settings
from designops.core.enums import SendMode


@dataclass(slots=True)
class DeliveryResult:
    status: str  # not_sent | self | draft | sent | blocked_go_live | failed
    message_id: str | None = None
    note: str | None = None


_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.S)
_WS_RUN_RE = re.compile(r"\s{2,}")
# Gmail clips messages at ~102KB; leave headroom for MIME/base64 overhead.
_GMAIL_CLIP_BYTES = 96 * 1024


def _minify_html(html: str) -> str:
    html = _HTML_COMMENT_RE.sub("", html)
    return _WS_RUN_RE.sub(" ", html).strip()


def prepare_email_html(html: str) -> str:
    """Make report HTML robust for Gmail before sending.

    Gmail clips messages over ~102KB, and its "View entire message" page strips
    <style> blocks, which breaks class-based styling. Strategy:
    - Minify (comments + whitespace). If that fits under the clip limit, send
      as-is — no clip banner, <style> works in the normal view.
    - If it can't fit, the clip is unavoidable — inline all CSS into style
      attributes so the opened full message still renders correctly (the
      <style> tag is kept for @media rules in the normal view).
    Never raises — a plain send beats a failed one.
    """
    minified = _minify_html(html)
    if len(minified.encode("utf-8")) <= _GMAIL_CLIP_BYTES:
        return minified
    try:
        import css_inline

        inlined = css_inline.CSSInliner(keep_style_tags=True).inline(html)
        return _minify_html(inlined)
    except Exception:  # noqa: BLE001 — send unmodified rather than fail delivery
        return minified


def send_via_smtp(
    recipients: list[str],
    subject: str,
    html: str,
    *,
    settings: Settings | None = None,
    status_label: str = "sent",
) -> DeliveryResult:
    """Send an HTML email over SMTP (Gmail app password — the simplest sender). Never
    raises on a transport/auth problem; returns status='failed' with the reason so the
    UI can show it."""
    s = settings or get_settings()
    if not s.smtp_configured:
        return DeliveryResult(
            status="failed",
            note="SMTP not configured — set GMAIL_SENDER and GMAIL_APP_PASSWORD in .env.",
        )
    if not recipients:
        return DeliveryResult(status="failed", note="no recipient")
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = s.gmail_sender
    msg["To"] = ", ".join(recipients)
    msg["Date"] = formatdate(localtime=True)
    mid = make_msgid()
    msg["Message-ID"] = mid
    msg.attach(MIMEText(html, "html", "utf-8"))
    try:
        with smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=30) as server:
            server.starttls()
            server.login(s.gmail_sender, s.gmail_app_password)
            server.send_message(msg)
    except Exception as e:  # noqa: BLE001 — surface the reason, don't crash the request
        return DeliveryResult(status="failed", note=f"{type(e).__name__}: {e}")
    return DeliveryResult(
        status=status_label, message_id=mid, note=f"sent to {', '.join(recipients)}"
    )


def send_digest(
    recipients: list[str],
    subject: str,
    html: str,
    *,
    settings: Settings | None = None,
    status_label: str = "sent",
) -> DeliveryResult:
    """Send by whichever transport is configured — Google OAuth (Gmail API) preferred,
    SMTP app password as fallback. Never raises; returns status='failed' with the reason."""
    s = settings or get_settings()
    html = prepare_email_html(html)
    from designops.adapters import google_oauth

    if google_oauth.is_connected(s):
        try:
            mid = google_oauth.send_gmail(recipients, subject, html, settings=s)
            return DeliveryResult(
                status=status_label, message_id=mid,
                note=f"sent to {', '.join(recipients)} via Gmail (OAuth)",
            )
        except Exception as e:  # noqa: BLE001 — surface, don't crash the request
            return DeliveryResult(status="failed", note=f"Gmail API: {type(e).__name__}: {e}")
    return send_via_smtp(recipients, subject, html, settings=s, status_label=status_label)


def deliver(
    *,
    go_live: bool,
    send_mode: str,
    html: str,
    recipients: list[str],
    subject: str,
    setup_owner_email: str,
) -> DeliveryResult:
    """Return what WAS done. Never raises on a config problem — it refuses safely."""
    # HARD GATE — nothing is delivered while go_live is false, regardless of send_mode.
    if not go_live:
        return DeliveryResult(
            status="blocked_go_live",
            note="go_live=false — digest rendered in-app only, nothing sent (§12).",
        )

    mode = SendMode(send_mode)
    if mode is SendMode.NONE:
        return DeliveryResult(status="not_sent", note="send_mode=none")
    if mode is SendMode.SELF:
        return _gmail_send([setup_owner_email], subject, html, label="self")
    if mode is SendMode.DRAFT:
        return _gmail_draft(recipients, subject, html)
    if mode is SendMode.SEND:
        return _gmail_send(recipients, subject, html, label="sent")
    return DeliveryResult(status="not_sent", note=f"unknown send_mode {send_mode!r}")


# --- Gmail transport: stubs in v1 (delivery is dormant, §6.5). Wired when a Gmail
#     connector + go_live sign-off exist (§12.4). Kept behind deliver()'s gate so
#     these can never fire while go_live is false. ---------------------------------

def _gmail_draft(recipients: list[str], subject: str, html: str) -> DeliveryResult:
    raise NotImplementedError(
        "Gmail draft delivery is not wired in v1. Enable only after the §12.4 "
        "promotion gate (5 validated unattended runs + Olga sign-off)."
    )


def _gmail_send(recipients: list[str], subject: str, html: str, *, label: str) -> DeliveryResult:
    """Send via Gmail OAuth or SMTP — same path as the run-page email button."""
    return send_digest(recipients, subject, html, status_label=label)
