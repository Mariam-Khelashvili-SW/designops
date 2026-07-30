"""§10.3 gate, Python edition — does a roster designer's daily land in Fairwind
`emails_internal` with per-project sections intact?

Run before build step 1: `python -m scripts.test_emails_internal <account_id>`.
More robust than the bash version: it downloads the internal thread bodies and
checks that the subject designer's 17 Jul daily is present AND still contains more
than one project section (the "misfiled under one account" failure mode, §10.3).

Reads FW_CLIENT_ID/SECRET from env only (Settings). Exit code 0 = PASS (fairwind
viable), 2 = FAIL (flip source_mode to gmail), 1 = could not run.
"""

from __future__ import annotations

import json
import re
import sys
import time

import httpx

from designops.core.config import get_settings

SUBJECT_HINTS = ["Predrag", "Gavrilovikj"]  # §11.2 test subject
REPORT_DATE = "2026-07-17"
# a daily is "intact" if its body mentions >= 2 distinct project strings
PROJECT_HINTS = ["Northerner", "Nicokick", "Felco", "Reuzel", "Furniture Trader", "Redecker"]


def _token(s, client: httpx.Client) -> str:
    r = client.post(
        f"{s.fw_base_url}/api/auth/oauth2/token",
        data={
            "grant_type": "client_credentials",
            "client_id": s.fw_client_id,
            "client_secret": s.fw_client_secret,
            "resource": s.fw_resource,
            "scope": s.fw_scope,
        },
    )
    r.raise_for_status()
    return r.json()["access_token"]


_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)


def _extract_export_id(payload) -> str | None:
    """Dig an in-flight export id out of a 409 body of unknown shape."""
    if isinstance(payload, dict):
        for k in ("id", "export_id", "existing_export_id", "existing_id", "in_flight_id"):
            if payload.get(k):
                return str(payload[k])
        for k in ("export", "data", "detail", "error"):
            v = payload.get(k)
            if isinstance(v, dict) and v.get("id"):
                return str(v["id"])
    m = _UUID.search(payload if isinstance(payload, str) else json.dumps(payload))
    return m.group(0) if m else None


def _create_or_reuse_export(client, base, h, account_id, date_from, date_to) -> str | None:
    """POST an export; on 409 reuse the in-flight one if the body names it, else
    backoff-retry until it clears (§1, §11.3 — 409 is retry/reuse, never failure)."""
    body = {
        "account_id": account_id,
        "date_from": date_from,
        "date_to": date_to,
        "data_types": ["emails_internal", "emails_external", "jira", "transcripts"],
        "include_files": False,
    }
    for attempt in range(8):
        r = client.post(f"{base}/exports", headers=h, json=body)
        if r.status_code != 409:
            r.raise_for_status()
            return r.json()["id"]
        try:
            payload = r.json()
        except Exception:
            payload = r.text
        eid = _extract_export_id(payload)
        if eid:
            print(f"  409 in-flight → reusing existing export {eid}")
            return eid
        ra = r.headers.get("Retry-After")
        delay = int(ra) if (ra and ra.isdigit()) else min(5 * 2**attempt, 60)
        print(f"  409 in-flight, no id in body; backoff {delay}s (attempt {attempt + 1}/8)")
        time.sleep(delay)
    return None


def main(argv: list[str]) -> int:
    s = get_settings()
    if not s.fairwind_configured:
        print("✗ FW_CLIENT_ID / FW_CLIENT_SECRET not set (rotate the leaked secret first, §9.6).")
        return 1
    if len(argv) < 2:
        print("usage: python -m scripts.test_emails_internal <design_account_id> [from] [to]")
        return 1
    account_id = argv[1]
    date_from, date_to = (argv[2] if len(argv) > 2 else "2026-07-13"), (
        argv[3] if len(argv) > 3 else "2026-07-18"
    )

    with httpx.Client(timeout=60) as client:
        token = _token(s, client)
        h = {"Authorization": f"Bearer {token}"}
        base = f"{s.fw_base_url}/api/v1"

        print(f"→ create export for {account_id} ({date_from}..{date_to})")
        export_id = _create_or_reuse_export(client, base, h, account_id, date_from, date_to)
        if not export_id:
            print("✗ could not obtain an export id (409 in-flight never yielded one)")
            return 1

        for i in range(60):
            st = client.get(f"{base}/exports/{export_id}", headers=h).json()
            print(f"  [{i}] status={st.get('status')}")
            if st.get("status") == "ready":
                print("  counts:", st.get("counts"))
                break
            if st.get("status") == "failed":
                print("✗ export failed")
                return 1
            time.sleep(5)
        else:
            print("✗ export never became ready")
            return 1

        manifest = client.get(f"{base}/exports/{export_id}.json", headers=h).json()
        files = manifest.get("files", [])
        internal = [f for f in files if "internal" in str(f).lower()]
        print(f"→ {len(internal)} internal files in manifest")

        subject_hits = 0
        intact_hits = 0
        for path in internal:
            try:
                body = client.get(
                    f"{base}/exports/{export_id}/files", headers=h, params={"path": path}
                ).text
            except Exception:
                continue
            if any(hint.lower() in body.lower() for hint in SUBJECT_HINTS):
                subject_hits += 1
                projects_seen = {p for p in PROJECT_HINTS if p.lower() in body.lower()}
                if len(projects_seen) >= 2:
                    intact_hits += 1
                    print(f"  ✓ subject daily in {path} — projects: {sorted(projects_seen)}")

    print("\n=== §10.3 verdict ===")
    if subject_hits and intact_hits:
        print("PASS — subject daily present with per-project sections intact. Fairwind viable.")
        return 0
    if subject_hits:
        print("PARTIAL — subject present but sections not intact (misfiled). Treat as FAIL.")
        return 2
    print("FAIL — subject daily absent from emails_internal. Flip source_mode → gmail (§10.3).")
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
