"""Unit tests for weekly-health Figma comment queue (no network)."""

from __future__ import annotations

from datetime import date

from designops.pipelines.weekly_health_figma import (
    attach_figma_to_cards,
    build_figma_project_output,
    build_figma_roster,
    classify_thread,
    fetch_figma_comments_bundle,
    figma_excerpt_no_activity,
    format_figma_excerpt,
    format_thread,
    is_client_author,
    is_internal_author,
    is_imperative_todo,
    is_internal_handle,
    is_question_shaped,
    load_figma_roster,
    looks_internal_handle,
    prefetch_figma_for_projects,
    sum_figma_overdue_kpi,
    thread_in_health_window,
    thread_sort_key,
)


def _thread(
    *,
    tid: str,
    message: str,
    created: str,
    unresolved: bool = True,
    user: str = "client.pm",
    user_email: str | None = "pm@acer.com",
    replies: list | None = None,
    client_meta: dict | None = None,
) -> dict:
    return {
        "root": {
            "id": tid,
            "message": message,
            "user": user,
            "user_email": user_email,
            "created_at": created,
            "resolved": not unresolved,
            "resolved_at": None if unresolved else created,
            "client_meta": client_meta or {"node_id": "1:2"},
        },
        "replies": replies or [],
        "unresolved": unresolved,
    }


def test_thread_in_health_window():
    since = date(2026, 8, 4)
    open_old = _thread(tid="1", message="still open", created="2026-01-01T00:00:00Z")
    assert thread_in_health_window(open_old, since) is True

    resolved_old = _thread(
        tid="3",
        message="done",
        created="2026-07-01T00:00:00Z",
        unresolved=False,
    )
    assert thread_in_health_window(resolved_old, since) is False


def test_classify_unanswered_client_thread():
    roster = load_figma_roster()
    thread = _thread(
        tid="1",
        message="Please approve the header",
        created="2026-08-09T10:00:00Z",
        user="Alessio",
        user_email=None,
    )
    assert classify_thread(thread, roster=roster) == "UNANSWERED"


def test_classify_imperative_internal_todo():
    roster = load_figma_roster()
    thread = _thread(
        tid="1",
        message="Remove delivery options",
        created="2026-08-03T10:00:00Z",
        user="Tamari Giunashvili",
        user_email="tamari@scandiweb.com",
    )
    assert classify_thread(thread, roster=roster) == "NOT_FINISHED"


def test_classify_skips_resolved():
    roster = load_figma_roster()
    thread = _thread(
        tid="1",
        message="Done",
        created="2026-08-09T10:00:00Z",
        unresolved=False,
    )
    assert classify_thread(thread, roster=roster) is None


def test_is_question_shaped_requires_word_count():
    assert not is_question_shaped("same as SGD?")
    assert is_question_shaped(
        "Need to clarify if they expect us showing here the lead times"
    )


def test_is_imperative_todo():
    assert is_imperative_todo("Remove delivery options")
    assert not is_imperative_todo("Looks good to me")


def test_format_thread_includes_status():
    thread = _thread(
        tid="1",
        message="Please revise spacing",
        created="2026-08-10T12:00:00Z",
    )
    text = format_thread(thread, file_label="abc123")
    assert "[OPEN]" in text
    assert "Please revise spacing" in text


def test_internal_vs_client_author():
    internal = {"user": "Kirill Rogovets", "user_email": "kirill@scandiweb.com"}
    client = {"user": "Alessio", "user_email": None}
    assert is_internal_author(internal) is True
    assert is_client_author(client) is True


def test_quote_authors_include_reply_writer():
    roster = load_figma_roster()
    thread = _thread(
        tid="1",
        message="Is this a slider or static?",
        created="2026-08-14T10:00:00Z",
        user="Sanna Purho",
        user_email=None,
        replies=[
            {
                "id": "r1",
                "message": "Sanna Purho this is intended as static, to replace the rotating carousel.",
                "user": "Maarja",
                "user_email": None,
                "created_at": "2026-08-14T11:00:00Z",
            }
        ],
    )
    from designops.pipelines.weekly_health_figma import _item_from_thread

    item = _item_from_thread(
        thread,
        kind="UNANSWERED",
        file_key="abc",
        file_url="https://www.figma.com/design/abc",
        as_of=date(2026, 8, 14),
        roster=roster,
    )
    assert item["who"] == "Sanna Purho"
    assert item["quotes"][0]["who"] == "Sanna Purho"
    assert "slider" in item["quotes"][0]["text"]
    assert item["quotes"][1]["who"] == "Maarja"
    assert "static" in item["quotes"][1]["text"]


def test_looks_internal_handle_sw_dev_and_scandiweb():
    assert looks_internal_handle("sw-dev")
    assert looks_internal_handle("SW-Dev")
    assert looks_internal_handle("scandiweb-bot")
    assert not looks_internal_handle("Alessio")
    assert not looks_internal_handle("Maarja Truu")


def test_designer_list_and_sw_dev_are_internal():
    roster = build_figma_roster(designer_names=["Elene Chekurishvili"])
    assert is_internal_handle("Elene Chekurishvili", roster)
    assert is_internal_handle("sw-dev", roster)
    assert is_internal_handle("elene chekurishvili", roster)  # casefold

    threads = [
        (
            "file",
            _thread(
                tid="1",
                message="ping",
                created="2026-08-10T00:00:00Z",
                user="sw-dev",
                user_email=None,
                unresolved=False,
            ),
        ),
        (
            "file",
            _thread(
                tid="2",
                message="Remove delivery options",
                created="2026-08-10T00:00:00Z",
                user="Elene Chekurishvili",
                user_email=None,
            ),
        ),
    ]
    panel = build_figma_project_output(
        threads,
        all_comments=[],
        as_of=date(2026, 8, 11),
        week_start=date(2026, 8, 4),
        url_by_key={"file": "https://www.figma.com/design/file"},
        roster=roster,
    )
    assert "unclassified_handles" not in panel
    assert panel["counts"]["still_open"] >= 1


def test_fetch_figma_comments_bundle_no_urls():
    bundle = fetch_figma_comments_bundle([], since=date(2026, 8, 4))
    assert "no Figma files linked" in bundle["excerpt"]
    assert bundle["files"] == 0


def test_fetch_figma_comments_bundle_with_threads(monkeypatch):
    since = date(2026, 8, 4)
    as_of = date(2026, 8, 11)
    raw_threads = [
        _thread(
            tid="open",
            message="Can you check comment, is it possible to set custom color in page builder for such blocks?",
            created="2026-07-27T00:00:00Z",
            user="Artur B",
            user_email="arturs@scandiweb.com",
            replies=[
                {
                    "id": "r1",
                    "message": "Svitlana Madei can you check",
                    "user": "Artur B",
                    "user_email": "arturs@scandiweb.com",
                    "created_at": "2026-07-27T01:00:00Z",
                }
            ],
        ),
        _thread(
            tid="resolved",
            message="ancient",
            created="2026-01-01T00:00:00Z",
            unresolved=False,
        ),
    ]

    monkeypatch.setattr("designops.adapters.figma.is_ready", lambda settings=None: True)

    def fake_fetch(url, *, settings=None, unresolved_only=False, as_md=True, limit=None):
        return {
            "file_key": "FileKey123",
            "threads": raw_threads,
            "comments": [
                {
                    "id": "open",
                    "message": "Can you check comment",
                    "user": {"handle": "Artur B"},
                    "created_at": "2026-07-27T00:00:00Z",
                    "parent_id": None,
                    "resolved_at": None,
                }
            ],
            "total": 2,
        }

    monkeypatch.setattr(
        "designops.adapters.figma.fetch_file_comments",
        fake_fetch,
    )

    bundle = fetch_figma_comments_bundle(
        ["https://www.figma.com/design/FileKey123/Test"],
        since=since,
        as_of=as_of,
    )
    panel = bundle["panel"]
    assert bundle["files"] == 1
    assert panel["counts"]["still_open"] >= 1
    assert "new" in bundle["excerpt"].lower() or "still open" in bundle["excerpt"].lower()


def test_fetch_figma_comments_bundle_no_comments(monkeypatch):
    since = date(2026, 8, 4)

    monkeypatch.setattr("designops.adapters.figma.is_ready", lambda settings=None: True)

    def fake_fetch(url, *, settings=None, unresolved_only=False, as_md=True, limit=None):
        return {
            "file_key": "k",
            "threads": [],
            "comments": [],
            "total": 0,
        }

    monkeypatch.setattr("designops.adapters.figma.fetch_file_comments", fake_fetch)

    bundle = fetch_figma_comments_bundle(
        ["https://www.figma.com/design/k/Test"],
        since=since,
    )
    assert bundle["excerpt"] == "No comments in the file."


def test_prefetch_figma_for_projects_not_configured(monkeypatch):
    monkeypatch.setattr(
        "designops.adapters.figma.is_ready",
        lambda settings=None: False,
    )
    proj = type(
        "P",
        (),
        {
            "canonical_name": "Acer",
            "figma_urls": ["https://www.figma.com/design/abc/Acer"],
        },
    )()
    coverage: dict = {}
    out = prefetch_figma_for_projects(
        [proj],
        comms_from=date(2026, 8, 4),
        coverage=coverage,
    )
    assert "not configured" in out["Acer"]["excerpt"]
    assert "figma_note" in coverage


def test_attach_figma_to_cards_uses_panel():
    cards = [{"display_name": "Acer"}]
    attach_figma_to_cards(
        cards,
        {
            "Acer": {
                "panel": {
                    "panel": "data",
                    "has_comments": True,
                    "counts": {
                        "new_comments": 2,
                        "still_open": 1,
                        "overdue_items": 1,
                    },
                    "overdue": [
                        {
                            "who": "Artur B",
                            "date_label": "27 Jul",
                            "quote": "Can you check custom color",
                            "age_working_days": 11,
                            "link": "https://www.figma.com/design/abc?node-id=1-2",
                            "quotes": ["Can you check custom color"],
                            "kind": "UNANSWERED",
                        }
                    ],
                    "this_week": [],
                }
            }
        },
    )
    assert cards[0]["figma"]["counts"]["still_open"] == 1
    assert cards[0]["figma"]["overdue"][0]["quote"] == "Can you check custom color"


def test_sum_figma_overdue_kpi():
    total = sum_figma_overdue_kpi(
        {
            "Acer": {"panel": {"counts": {"overdue_items": 2}}},
            "Tobis": {"panel": {"counts": {"overdue_items": 4}}},
        }
    )
    assert total == 6


def test_format_figma_excerpt_no_activity():
    text = format_figma_excerpt(
        {"panel": "empty", "counts": {"still_open": 0}, "has_comments": False},
        since=date(2026, 8, 4),
    )
    assert text == figma_excerpt_no_activity(date(2026, 8, 4))
