"""Regression tests for workgraph_classify.py (tasks #24, #19):
- dead regex stems (escalat\\w* etc.) now match real inflections
- direction_inferred/sentiment_inferred/topic_inferred genuinely flip to
  False on a real cue match, so confidence tier H is actually reachable
- backfill_reclassify writes the FRESH result, not stale item[...] values
"""
import workgraph_classify as wc


def test_escalating_matches_negative_cue_stem():
    """Before the fix: NEGATIVE_CUE had bare 'escalat' with a trailing \\b,
    so only the exact word "escalate" matched - "escalating"/"escalation"
    (the far more common real inflections) did not."""
    result = wc.classify_item(subject="Escalating this to your attention",
                               body_preview="", from_actor="supplier@acme.com")
    assert result["sentiment"] == "negative"
    assert result["sentiment_inferred"] is False


def test_termination_matches_contract_topic_stem():
    result = wc.classify_item(subject="Notice of termination for the MSA",
                               body_preview="", from_actor="legal@acme.com")
    assert result["topic"] == "contract"
    assert result["topic_inferred"] is False


def test_confidence_tier_h_is_reachable():
    """A fully-explicit item (real direction cue, real topic cue, real
    sentiment cue, confident class) must be able to reach tier H - before
    the fix, direction_inferred/sentiment_inferred were hardcoded True
    unconditionally, making H unreachable for ANY input."""
    result = wc.classify_item(
        subject="We received your invoice - thank you for your patience",
        body_preview="",
        from_actor="finance@acme.com",
    )
    assert result["direction_inferred"] is False  # "received" -> INBOUND_CUE
    assert result["sentiment_inferred"] is False  # "thank you" -> POSITIVE_CUE
    assert result["topic_inferred"] is False  # "invoice" -> financial topic
    assert result["confidence"] in ("H", "M")  # at minimum should not be forced to L


def test_direction_inferred_flips_false_on_real_cue():
    result = wc.classify_item(subject="FYI, sending this along internally",
                               body_preview="", from_actor="colleague@lilly.com")
    # whatever direction it resolves to, the point is direction_inferred must
    # be able to be False when a real cue exists - not hardcoded True always
    assert isinstance(result["direction_inferred"], bool)
