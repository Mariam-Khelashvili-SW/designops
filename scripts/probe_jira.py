"""Probe Jira Cloud credentials from env. Prints displayName + one sample search count.

Usage: python -m scripts.probe_jira
Never prints the API token.
"""

from __future__ import annotations

from designops.adapters.jira import JiraClient
from designops.core.config import get_settings


def main() -> None:
    get_settings.cache_clear()
    s = get_settings()
    if not s.jira_configured:
        print("FAIL: JIRA_BASE_URL / JIRA_EMAIL / JIRA_API_TOKEN not all set")
        raise SystemExit(1)
    print(f"base: {s.jira_base_url.rstrip('/')}")
    print(f"email: {s.jira_email}")
    client = JiraClient(s)
    me = client.myself()
    print(f"auth ok: {me.get('displayName')} (accountId={me.get('accountId')})")
    issues = client.search_jql(
        "assignee = currentUser() AND statusCategory != Done ORDER BY updated DESC",
        max_results=5,
    )
    print(f"sample open issues for currentUser: {len(issues)}")
    for i in issues[:3]:
        key = i.get("key")
        summary = (i.get("fields") or {}).get("summary", "")
        print(f"  - {key}: {summary[:80]}")
    print("OK")


if __name__ == "__main__":
    main()
