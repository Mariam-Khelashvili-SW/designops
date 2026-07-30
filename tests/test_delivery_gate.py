"""§12.4 — the go_live gate must block delivery in code, for every send_mode."""

from __future__ import annotations

import pytest

from designops.adapters.delivery import _GMAIL_CLIP_BYTES, deliver, prepare_email_html


@pytest.mark.parametrize("send_mode", ["none", "self", "draft", "send"])
def test_go_live_false_blocks_every_send_mode(send_mode):
    result = deliver(
        go_live=False,
        send_mode=send_mode,
        html="<h1>digest</h1>",
        recipients=["olga@scandiweb.com"],
        subject="Daily Ops Digest",
        setup_owner_email="liana.staskevica@scandiweb.com",
    )
    assert result.status == "blocked_go_live"
    assert result.message_id is None


def test_go_live_true_send_mode_none_still_sends_nothing():
    result = deliver(
        go_live=True,
        send_mode="none",
        html="<h1>digest</h1>",
        recipients=["olga@scandiweb.com"],
        subject="Daily Ops Digest",
        setup_owner_email="liana.staskevica@scandiweb.com",
    )
    assert result.status == "not_sent"


def _report(rows: int) -> str:
    body = "".join(
        f'<tr><td class="who">Person {i}</td><td class="num">{i}h</td></tr>'
        for i in range(rows)
    )
    return (
        "<html><head><style>"
        ".who{font-weight:700;color:#2b3038}.num{text-align:right}"
        "@media(max-width:600px){.num{display:none}}"
        "</style></head><body>  <!-- comment -->  "
        f"<table>{body}</table></body></html>"
    )


def test_prepare_email_html_small_stays_minified():
    # Under Gmail's clip limit → no CSS inlining, comments/whitespace stripped.
    out = prepare_email_html(_report(rows=5))
    assert len(out.encode()) <= _GMAIL_CLIP_BYTES
    assert "<!--" not in out
    assert '<td class="who" style=' not in out  # classes untouched
    assert "<style" in out


def test_prepare_email_html_oversized_gets_inlined():
    # Over the clip limit Gmail strips <style> in the full-message view, so the
    # class CSS must be inlined onto elements; <style> is kept for @media rules.
    out = prepare_email_html(_report(rows=3000))
    assert '<td class="who" style="' in out
    assert "font-weight" in out.split('<td class="who" style="', 1)[1][:120]
    assert "<style" in out


def test_go_live_true_draft_raises_not_wired_in_v1():
    # delivery is dormant in v1; the transport must refuse rather than silently no-op
    with pytest.raises(NotImplementedError):
        deliver(
            go_live=True,
            send_mode="draft",
            html="<h1>digest</h1>",
            recipients=["olga@scandiweb.com"],
            subject="Daily Ops Digest",
            setup_owner_email="liana.staskevica@scandiweb.com",
        )
