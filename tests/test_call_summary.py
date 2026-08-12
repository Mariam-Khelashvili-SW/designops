"""Unit tests for call-summary pure helpers (no Anthropic / no DB)."""

from datetime import date, datetime, timedelta, timezone

from designops.pipelines.call_scope import is_external_call, is_internal_email
from designops.pipelines.call_summary import (
    assess_transcript_quality,
    build_review_table,
    compute_thanks_line,
    count_placeholders,
    dice_coefficient,
    evidence_matches_transcript,
    find_disallowed_placeholders,
    merge_critic_into_extraction,
    run_policy_guard,
    skeleton_composition,
    validate_composition_draft,
    validate_extraction_evidence,
)


def test_is_internal_email():
    assert is_internal_email("alex@scandiweb.com")
    assert is_internal_email("x@scandipwa.com")
    assert not is_internal_email("client@acer.com")


def test_is_external_call():
    assert is_external_call(
        [{"email": "a@scandiweb.com"}, {"email": "c@client.com"}],
        None,
    )
    assert not is_external_call([{"email": "a@scandiweb.com"}], None)
    assert is_external_call([{"email": "a@scandiweb.com"}], ["c@client.com"])


def test_quality_gate_degraded_fragments():
    text = "\n".join(["ok", "hi", "yes", "mm", "uh", "configurator", "x"])
    q = assess_transcript_quality(text)
    assert q.low_confidence is True


def test_quality_gate_good():
    text = "\n".join(
        [
            "We should move the FAQ above the contact form as discussed.",
            "The client agreed to add a low on stock label to product cards.",
            "Next call will be next Thursday after the designs are ready.",
        ]
    )
    q = assess_transcript_quality(text)
    assert q.low_confidence is False


def test_dice_and_evidence():
    assert dice_coefficient("hello world", "hello world") == 1.0
    assert evidence_matches_transcript("low on stock", "please add a low on stock label")
    assert not evidence_matches_transcript("completely invented phrase xyz", "short transcript here")


def test_stitched_evidence_matches():
    transcript = "we decided to shut down B2C\nclient: yes that is correct\nok next topic"
    evidence = "we decided to shut down B2C / client: yes that is correct"
    assert evidence_matches_transcript(evidence, transcript)


def test_validate_drops_bad_evidence():
    extraction = {
        "decisions": [{"text": "x", "confidence": "high", "evidence": "not in transcript at all"}],
        "our_commitments": [],
        "client_actions": [],
        "open_questions": [],
        "flags": [],
        "artifacts": [],
        "next_meeting": {"date_iso": None, "timing_verbatim": None, "evidence": ""},
        "topics_discussed_unclear_outcome": [],
        "meeting": {
            "title": "",
            "date_iso": None,
            "attendees_client": [],
            "attendees_internal": [],
        },
        "transcript_quality": "good",
    }
    validated, dropped = validate_extraction_evidence(extraction, "we talked about checkout")
    assert dropped >= 1
    assert validated["decisions"] == []


def test_policy_guard_currency():
    ok, reasons = run_policy_guard(
        body="We will do this for €5000.",
        transcript_quality="good",
        source_text="no money mentioned",
        allowed_urls=[],
    )
    assert ok is False
    assert any("Currency" in r for r in reasons)


def test_placeholders():
    assert count_placeholders("See [Figma link] by [date] or [option 1]") == 3
    assert find_disallowed_placeholders("Hello [CONFIRM: this]") == ["[CONFIRM: this]"]
    assert find_disallowed_placeholders("See [option 2]") == []


def test_skeleton():
    c = skeleton_composition(
        project_name="Acme",
        date_label="2026-08-01",
        owner_name="Alex",
        client_names="Sam",
        thanks_line="Thank you for the call yesterday!",
    )
    assert "Acme - follow-up from our call" in c["subject"]
    assert "—" not in c["subject"]
    assert "Alex" in c["body"]
    assert "Thank you for the call yesterday!" in c["body"]
    assert "Hello Sam," in c["body"]


def test_thanks_line_same_day_and_yesterday():
    send = datetime(2026, 8, 12, 15, 0, tzinfo=timezone.utc)
    assert (
        compute_thanks_line(call_datetime=send, send_datetime=send)
        == "Thank you for the call earlier today!"
    )
    yesterday = send - timedelta(days=1)
    assert (
        compute_thanks_line(call_datetime=yesterday, send_datetime=send)
        == "Thank you for the call yesterday!"
    )
    midweek = date(2026, 8, 10)  # Monday if send is Wed 12th
    line = compute_thanks_line(call_datetime=midweek, send_datetime=date(2026, 8, 12))
    assert line.startswith("Thank you for the call on ")
    assert line.endswith("!")


def test_composition_validator_em_dash_and_banned():
    extraction = {
        "decisions": [],
        "our_commitments": [],
        "client_actions": [],
        "open_questions": [],
        "flags": [],
        "artifacts": [],
    }
    ok, reasons = validate_composition_draft(
        body="Hope this finds you well — thanks.",
        extraction=extraction,
    )
    assert ok is False
    assert any("Em dash" in r for r in reasons)
    assert any("Banned" in r for r in reasons)


def test_composition_validator_timing_and_urgent_flag():
    extraction = {
        "decisions": [],
        "our_commitments": [
            {
                "text": "Send adjusted frames",
                "timing_verbatim": "by the end of this week",
                "evidence": "we will send by the end of this week",
            }
        ],
        "client_actions": [],
        "open_questions": [],
        "flags": [
            {
                "text": "staging credentials exposed",
                "severity": "urgent",
                "evidence": "staging credentials exposed",
            }
        ],
        "artifacts": [],
    }
    body = (
        "Hello Sam,\n\n"
        "Thank you for the call!\n\n"
        "From our side we will proceed with:\n"
        "- Send adjusted frames\n\n"
        "- staging credentials exposed\n"
    )
    ok, reasons = validate_composition_draft(body=body, extraction=extraction)
    assert ok is False
    assert any("timing" in r.lower() for r in reasons)
    assert any("Urgent flag" in r for r in reasons)

    # Composition formats timing as a short suffix — long verbatim monologues must not fail.
    body_ok = (
        "Hello Sam,\n\n"
        "From our side we will proceed with:\n"
        "- Send adjusted frames - by the end of this week\n"
    )
    extraction_ok = {
        **extraction,
        "flags": [],
        "our_commitments": [
            {
                "text": "Send adjusted frames",
                "timing_verbatim": (
                    "Maybe not this week... starting next week, I will work on it "
                    "and update you next week"
                ),
                "evidence": "starting next week I will work on it",
            }
        ],
    }
    ok2, reasons2 = validate_composition_draft(
        body=body_ok.replace("by the end of this week", "next week"),
        extraction=extraction_ok,
    )
    assert ok2 is True, reasons2


def test_composition_validator_recap_impact_gate():
    extraction = {
        "decisions": [
            {
                "text": "Remove the VAT field",
                "impact": "detail",
                "reverses_prior_assumption": False,
                "evidence": "remove the vat field",
            },
            {
                "text": "Shut down the B2C storefront",
                "impact": "project",
                "reverses_prior_assumption": True,
                "evidence": "there is not going to be a b2c store",
            },
        ],
        "our_commitments": [],
        "client_actions": [],
        "open_questions": [],
        "flags": [],
        "artifacts": [],
    }
    # Bullet clearly about the detail decision → fail
    body_bad = (
        "We also want to confirm the main points we aligned on:\n"
        "- Remove the VAT field from checkout\n"
    )
    ok, reasons = validate_composition_draft(body=body_bad, extraction=extraction)
    assert ok is False
    assert any("low-impact" in r.lower() for r in reasons)

    # Bullet about the project decision must not false-positive on the detail item
    body_ok = (
        "We also want to confirm the main points we aligned on:\n"
        "- Shut down the B2C storefront\n"
    )
    ok2, reasons2 = validate_composition_draft(body=body_ok, extraction=extraction)
    assert ok2 is True, reasons2


def test_merge_critic_adds_and_reclassifies():
    transcript = (
        "we decided there is not going to be a B2C store / "
        "client confirmed yes shut it down"
    )
    extraction = {
        "decisions": [
            {
                "text": "Keep B2C for now",
                "impact": "single_screen",
                "reverses_prior_assumption": False,
                "evidence": "keep b2c for now",
            }
        ],
        "our_commitments": [],
        "client_actions": [],
        "open_questions": [],
        "flags": [],
        "artifacts": [],
        "topics_discussed_unclear_outcome": [],
    }
    critic = {
        "additions": [
            {
                "target_array": "decisions",
                "text": "Shut down B2C store",
                "impact": "project",
                "reverses_prior_assumption": True,
                "evidence": "we decided there is not going to be a B2C store / client confirmed yes shut it down",
            }
        ],
        "reclassifications": [
            {
                "decision_text": "Keep B2C for now",
                "field": "impact",
                "from": "single_screen",
                "to": "detail",
                "why": "was a detail only",
            }
        ],
        "downgrades": [],
    }
    merged, notes = merge_critic_into_extraction(extraction, critic, transcript)
    assert any(d.get("text") == "Shut down B2C store" for d in merged["decisions"])
    keep = next(d for d in merged["decisions"] if d.get("text") == "Keep B2C for now")
    assert keep["impact"] == "detail"
    assert notes


def test_explain_draft_status_skeleton_and_human_reasons():
    from designops.pipelines.call_summary import explain_draft_status

    status = explain_draft_status(
        body_text="Hello\n\nFollow-up details to be confirmed ([date])\n",
        policy_blocked=True,
        policy_block_reason=(
            "Commitment timing missing from body: next week; "
            "Recap includes low-impact decision: Remove VAT field"
        ),
        reviewer_notes=["Skeleton draft: fact sheet was empty or policy blocked regeneration."],
        transcript_quality="degraded",
        low_confidence=False,
        placeholder_count=1,
        extraction={"decisions": [{}, {}], "our_commitments": [{}], "client_actions": [], "open_questions": []},
    )
    assert status["level"] == "error"
    assert status["skeleton"] is True
    assert status["list_label"] == "placeholder"
    assert any("timing" in r.lower() for r in status["reasons"])
    assert status["fact_counts"]["decisions"] == 2
    extraction = {
        "decisions": [
            {
                "text": "Proceed with closed catalog",
                "impact": "project",
                "evidence": "we will proceed with closed catalog",
            }
        ],
        "our_commitments": [
            {
                "text": "Share adjusted Figma frames",
                "timing_verbatim": "this week",
                "evidence": "we will share adjusted frames this week",
            }
        ],
        "client_actions": [],
        "open_questions": [],
        "artifacts": [],
    }
    body = (
        "From our side we will proceed with:\n"
        "- Share adjusted Figma frames - this week\n\n"
        "We also want to confirm the main points we aligned on:\n"
        "- Proceed with closed catalog\n"
    )
    rows = build_review_table(body=body, extraction=extraction)
    assert len(rows) == 2
    assert rows[0]["matched"] is True
    assert rows[0]["source"] == "our_commitments"
    assert rows[1]["source"] == "decisions"
    assert rows[1]["impact"] == "project"
