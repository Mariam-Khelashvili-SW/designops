"""Jira project-key ranking for weekly-health allowlist."""

from designops.core.projects import resolve_jira_candidates
from designops.pipelines.weekly_health_math import enrich_jira_candidates_with_design_fit


def test_hoitolatukku_prefers_redesign_over_service_cloud():
    matches = resolve_jira_candidates(
        project_name="Hoitolatukku",
        fairwind_account_id="acct-1",
        account_keys=["HOITO"],
        jira_cloud_projects=[
            {"key": "HOITOR", "name": "Hoitolatukku | Redesign"},
            {"key": "HOITO", "name": "Service Cloud - Hoitolatukku"},
        ],
    )
    assert matches[0]["key"] == "HOITOR"
    by_key = {m["key"]: m for m in matches}
    assert by_key["HOITOR"]["score"] < by_key["HOITO"]["score"]


def test_account_key_prefix_does_not_auto_win():
    """HOITO ⊂ Hoitolatukku must not score as an exact match."""
    matches = resolve_jira_candidates(
        project_name="Hoitolatukku",
        fairwind_account_id=None,
        account_keys=["HOITO"],
    )
    assert matches[0]["key"] == "HOITO"
    assert matches[0]["score"] >= 2


def test_enrich_ranks_by_design_fit():
    class FakeClient:
        def search_jql(self, jql, **kwargs):
            if 'project = "HOITOR"' in jql:
                return [
                    {
                        "key": "HOITOR-1",
                        "fields": {
                            "summary": "Homepage",
                            "status": {"name": "To Do"},
                            "assignee": {"emailAddress": "maarja@scandiweb.com"},
                            "issuetype": {"name": "Task"},
                            "components": [],
                            "project": {"key": "HOITOR"},
                        },
                    }
                ]
            return [
                {
                    "key": "HOITO-1",
                    "fields": {
                        "summary": "Infra",
                        "status": {"name": "Done"},
                        "assignee": {"emailAddress": "dev@scandiweb.com"},
                        "issuetype": {"name": "Task"},
                        "components": [],
                        "project": {"key": "HOITO"},
                    },
                }
            ]

    matches = [
        {"key": "HOITO", "name": "Service Cloud", "source": "jira", "score": 2},
        {"key": "HOITOR", "name": "Redesign", "source": "jira", "score": 1},
    ]
    ranked = enrich_jira_candidates_with_design_fit(
        matches,
        client=FakeClient(),
        roster_emails={"maarja@scandiweb.com"},
    )
    assert ranked[0]["key"] == "HOITOR"
    assert ranked[0]["design_score"] == 100
    assert ranked[1]["design_score"] == 0
    assert "1/1 design" in ranked[0]["design_label"]
