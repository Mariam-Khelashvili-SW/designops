"""Unit tests for call-summary pure helpers (no Anthropic / no DB)."""

from designops.pipelines.call_scope import is_external_call, is_internal_email
from designops.pipelines.call_summary import (
    assess_transcript_quality,
    count_placeholders,
    dice_coefficient,
    evidence_matches_transcript,
    find_disallowed_placeholders,
    run_policy_guard,
    skeleton_composition,
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


def test_validate_drops_bad_evidence():
    extraction = {
        "decisions": [{"text": "x", "confidence": "high", "evidence": "not in transcript at all"}],
        "needs_client_approval": [],
        "next_steps": [],
        "next_meeting": {"date": None, "evidence": ""},
        "artifacts_mentioned": [],
        "topics_discussed_unclear_outcome": [],
        "meeting": {"title": "", "date": "", "attendees_client": [], "attendees_internal": []},
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
    assert count_placeholders("See [Figma link] by [date]") == 2
    assert find_disallowed_placeholders("Hello [CONFIRM: this]") == ["[CONFIRM: this]"]


def test_skeleton():
    c = skeleton_composition(project_name="Acme", date_label="2026-08-01", owner_name="Alex")
    assert "Acme" in c["subject"]
    assert "Alex" in c["body"]
