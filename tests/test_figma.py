"""Unit tests for Figma comment fetch helpers (no network)."""

from __future__ import annotations

from designops.adapters.figma import extract_file_key, normalize_comment, thread_comments


def test_extract_file_key_from_design_url():
    url = "https://www.figma.com/design/fA2bj1XNF1JiohrbdC6iqh/Foo?node-id=1-2"
    assert extract_file_key(url) == "fA2bj1XNF1JiohrbdC6iqh"


def test_extract_file_key_from_file_and_board_urls():
    assert (
        extract_file_key("https://figma.com/file/AbCdEfGhIjKlMnOp/Name")
        == "AbCdEfGhIjKlMnOp"
    )
    assert (
        extract_file_key("https://www.figma.com/board/XxYyZz1234567890/Jam")
        == "XxYyZz1234567890"
    )


def test_extract_file_key_bare():
    assert extract_file_key("fA2bj1XNF1JiohrbdC6iqh") == "fA2bj1XNF1JiohrbdC6iqh"
    assert extract_file_key("") is None
    assert extract_file_key("https://example.com/x") is None


def test_normalize_comment():
    n = normalize_comment(
        {
            "id": "c1",
            "message": "Please fix spacing",
            "user": {"handle": "mariam", "id": "u1"},
            "created_at": "2026-01-01T00:00:00Z",
            "resolved_at": None,
            "parent_id": None,
            "order_id": "1",
            "reactions": [],
        }
    )
    assert n["id"] == "c1"
    assert n["user"] == "mariam"
    assert n["resolved"] is False


def test_parse_figma_url_list_dedupes_and_strips_query():
    from designops.adapters.figma import parse_figma_url_list

    raw = """
    https://www.figma.com/design/eKpn8VTz18xYBdxyvvoetm/Hyva?node-id=5-4367
    https://www.figma.com/design/eKpn8VTz18xYBdxyvvoetm/Hyva
    https://www.figma.com/design/fA2bj1XNF1JiohrbdC6iqh/B2B
    """
    urls = parse_figma_url_list(raw)
    assert urls == [
        "https://www.figma.com/design/eKpn8VTz18xYBdxyvvoetm/Hyva",
        "https://www.figma.com/design/fA2bj1XNF1JiohrbdC6iqh/B2B",
    ]


def test_recent_comments_sorts_and_limits():
    from designops.adapters.figma import recent_comments

    raw = [
        {
            "id": "old",
            "message": "first",
            "user": {"handle": "a"},
            "created_at": "2026-01-01T00:00:00Z",
            "resolved_at": None,
            "parent_id": None,
        },
        {
            "id": "new",
            "message": "latest",
            "user": {"handle": "b"},
            "created_at": "2026-03-01T00:00:00Z",
            "resolved_at": None,
            "parent_id": None,
        },
        {
            "id": "mid",
            "message": "middle",
            "user": {"handle": "c"},
            "created_at": "2026-02-01T00:00:00Z",
            "resolved_at": "2026-02-02T00:00:00Z",
            "parent_id": None,
        },
    ]
    limited = recent_comments(raw, limit=2)
    assert [c["id"] for c in limited] == ["new", "mid"]
    assert limited[1]["resolved"] is True
    assert len(recent_comments(raw)) == 3


def test_parse_figma_url_list_rejects_non_figma():
    from designops.adapters.figma import FigmaError, parse_figma_url_list
    import pytest

    with pytest.raises(FigmaError):
        parse_figma_url_list("https://notion.so/page")

    raw = [
        {
            "id": "root1",
            "message": "open",
            "user": {"handle": "a"},
            "created_at": "2026-01-01T00:00:00Z",
            "resolved_at": None,
            "parent_id": None,
        },
        {
            "id": "reply1",
            "message": "ack",
            "user": {"handle": "b"},
            "created_at": "2026-01-01T01:00:00Z",
            "resolved_at": None,
            "parent_id": "root1",
        },
        {
            "id": "root2",
            "message": "done",
            "user": {"handle": "c"},
            "created_at": "2026-01-02T00:00:00Z",
            "resolved_at": "2026-01-03T00:00:00Z",
            "parent_id": None,
        },
    ]
    all_threads = thread_comments(raw)
    assert len(all_threads) == 2
    open_t = next(t for t in all_threads if t["root"]["id"] == "root1")
    assert len(open_t["replies"]) == 1
    assert open_t["replies"][0]["id"] == "reply1"
    assert open_t["unresolved"] is True

    only_open = thread_comments(raw, unresolved_only=True)
    assert len(only_open) == 1
    assert only_open[0]["root"]["id"] == "root1"
