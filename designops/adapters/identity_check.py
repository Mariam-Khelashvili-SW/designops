"""Identity checks for roster people — Jira user lookup + Fairwind email presence."""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path

from designops.adapters.jira import JiraClient
from designops.core.config import Settings, get_settings

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class IdentityCheckResult:
    email: str
    jira_ok: bool
    jira_account_id: str | None
    jira_display_name: str | None
    jira_error: str | None
    fairwind_ok: bool
    fairwind_detail: str
    verified: bool

    @property
    def summary(self) -> str:
        parts = []
        if self.jira_ok:
            who = self.jira_display_name or self.jira_account_id or "found"
            parts.append(f"Jira ✓ ({who})")
        elif self.jira_error:
            parts.append(f"Jira ✗ ({self.jira_error})")
        else:
            parts.append("Jira ✗ (no user for this email)")
        parts.append(
            f"Fairwind ✓ ({self.fairwind_detail})"
            if self.fairwind_ok
            else f"Fairwind ✗ ({self.fairwind_detail})"
        )
        return " · ".join(parts)


def fairwind_email_seen(email: str, settings: Settings | None = None) -> tuple[bool, str]:
    """True if the email appears in cached Fairwind export corpus (sender/assignee data).

    Fairwind has no people-directory API, so presence in pulled email/jira/transcript
    exports is the practical check that Fairwind knows this address.
    """
    s = settings or get_settings()
    root = Path(s.corpus_store_dir)
    if not root.exists():
        return False, "no Fairwind corpus on disk yet — run a digest/export first"
    email_l = email.strip().lower()
    if not email_l:
        return False, "empty email"
    try:
        proc = subprocess.run(
            [
                "rg",
                "-l",
                "-i",
                "--fixed-strings",
                "--glob",
                "*.json",
                "--glob",
                "*.jsonl",
                email_l,
                str(root),
            ],
            capture_output=True,
            text=True,
            timeout=45,
            check=False,
        )
    except FileNotFoundError:
        # Fallback without ripgrep
        for path in root.rglob("*.json"):
            try:
                if email_l in path.read_text(encoding="utf-8", errors="ignore").lower():
                    return True, "seen in Fairwind export corpus"
            except OSError:
                continue
        return False, "not found in Fairwind export corpus"
    except subprocess.TimeoutExpired:
        return False, "corpus search timed out"
    if proc.returncode == 0 and (proc.stdout or "").strip():
        return True, "seen in Fairwind export corpus"
    if proc.returncode in (0, 1):
        return False, "not found in Fairwind export corpus"
    logger.warning("rg corpus search failed: %s", proc.stderr)
    return False, "corpus search failed"


def check_identity(
    emails: list[str],
    *,
    settings: Settings | None = None,
) -> IdentityCheckResult:
    """Check emails against Jira (live) and Fairwind corpus. First matching email wins for Jira."""
    s = settings or get_settings()
    cleaned = [e.strip().lower() for e in emails if e and "@" in e]
    if not cleaned:
        return IdentityCheckResult(
            email="",
            jira_ok=False,
            jira_account_id=None,
            jira_display_name=None,
            jira_error="no email provided",
            fairwind_ok=False,
            fairwind_detail="no email provided",
            verified=False,
        )

    jira_ok = False
    jira_id = None
    jira_name = None
    jira_error = None
    matched_email = cleaned[0]
    try:
        client = JiraClient(s)
        if not s.jira_configured:
            jira_error = "Jira not configured"
        else:
            for email in cleaned:
                user = client.lookup_user(email)
                if user and user.get("accountId"):
                    jira_ok = True
                    jira_id = user.get("accountId")
                    jira_name = (
                        user.get("displayName")
                        or user.get("publicName")
                        or user.get("emailAddress")
                    )
                    matched_email = email
                    break
    except Exception as e:  # noqa: BLE001 — surface to UI
        jira_error = str(e)

    fw_ok = False
    fw_detail = "not checked"
    for email in cleaned:
        ok, detail = fairwind_email_seen(email, s)
        if ok:
            fw_ok = True
            fw_detail = detail
            break
        fw_detail = detail

    return IdentityCheckResult(
        email=matched_email,
        jira_ok=jira_ok,
        jira_account_id=jira_id,
        jira_display_name=jira_name,
        jira_error=jira_error,
        fairwind_ok=fw_ok,
        fairwind_detail=fw_detail,
        verified=jira_ok and fw_ok,
    )
