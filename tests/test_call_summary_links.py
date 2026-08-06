"""Tests for call-scoped Jira link resolution."""

from designops.pipelines.call_summary_links import (
    inject_resolved_urls_into_body,
    prefer_jira_keys,
    resolve_call_jira_key,
)


KEYS = ["SGD", "SGDB2B", "SGDCP", "SGDP1"]


def test_b2b_call_uses_only_sgdb2b():
    key = resolve_call_jira_key(
        KEYS,
        meeting_title="SGD & scandiweb: B2B weekly updates - 2026/08/04",
    )
    assert key == "SGDB2B"
    assert prefer_jira_keys(KEYS, meeting_title="B2B weekly") == ["SGDB2B"]


def test_club_portal_call_uses_sgdcp():
    assert (
        resolve_call_jira_key(KEYS, meeting_title="SGD Club Portal design review")
        == "SGDCP"
    )


def test_ambiguous_title_returns_none():
    # Must not fall back to searching every board on the account
    assert resolve_call_jira_key(KEYS, meeting_title="Sports Group Denmark sync") is None
    assert prefer_jira_keys(KEYS, meeting_title="Sports Group Denmark sync") == []


def test_artifacts_do_not_widen_scope():
    # Even if artifacts mention club portal, B2B meeting stays on SGDB2B
    keys = prefer_jira_keys(
        KEYS,
        meeting_title="SGD B2B weekly",
        artifact_names=["Wireframes for rocket group club portal", "Project road map"],
    )
    assert keys == ["SGDB2B"]


def test_fairwind_board_name_match():
    fw = [
        {"key": "SGD", "name": "Service Cloud - Sports Group Denmark"},
        {"key": "SGDB2B", "name": "Sports Group Denmark B2B"},
        {"key": "SGDCP", "name": "Sports Group Denmark | Club Portal | Phase 1"},
    ]
    assert (
        resolve_call_jira_key(
            KEYS,
            meeting_title="Weekly B2B check-in with Sports Group",
            fairwind_jira_projects=fw,
        )
        == "SGDB2B"
    )


def test_inject_replaces_placeholder_on_matching_line():
    body = (
        "Thanks.\n\n"
        "**Pending**\n"
        "- Pass the wireframe link: [link]\n"
        "Project road map for reference: [link]\n"
    )
    link_map = {
        "wireframe link": "https://www.figma.com/design/abc/B2B",
        "Project road map": "https://www.notion.so/scandiweb/B2B-page",
        "notion_brief": "https://www.notion.so/scandiweb/B2B-page",
    }
    arts = [
        {"name": "wireframe link", "platform": "figma"},
        {"name": "Project road map", "platform": "other"},
    ]
    out = inject_resolved_urls_into_body(body, link_map, arts)
    assert "figma.com" in out
    assert "notion.so" in out
    assert out.count("[link]") == 0
