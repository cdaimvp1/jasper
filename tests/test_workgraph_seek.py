"""Tests for workgraph_seek (tasks #396 source enumeration, #397 question
generation).

The functions under test are pure - they take already-read party rows - so this
file needs no DB at all. That is deliberate on the module's side and is what
makes these tests fast and hermetic.

The three rules in the module docstring are what actually matter here, so each
gets an explicit guard rather than being implied by a behaviour test.
"""
from __future__ import annotations

import workgraph_ambiguity as amb
import workgraph_seek as seek

EXTERNAL = [{"primary_email": "rep@kinaxis.com", "affiliation": "external"}]
INTERNAL_ONLY = [{"primary_email": "colleague@lilly.com", "affiliation": "internal"}]


def _gap(kind, ref=None):
    return amb.Gap(kind=kind, what=f"{kind} happened", fillable_by="somewhere", ref=ref)


# ------------------------------------------------- RULE 1: never invent a recipient

def test_no_external_party_means_no_questions_at_all():
    """The load-bearing rule. Guessing a recipient is how an assistant emails
    the wrong person about a contract."""
    gaps = [_gap("unresolved_reference", "PR123"), _gap("stale_evidence")]
    assert seek.generate_questions(gaps, parties=[]) == []
    assert seek.generate_questions(gaps, parties=INTERNAL_ONLY) == []


def test_every_generated_question_names_a_real_recipient():
    gaps = [_gap("unresolved_reference", "PR123"), _gap("stale_evidence")]
    qs = seek.generate_questions(gaps, parties=EXTERNAL)
    assert qs
    for q in qs:
        assert q.asked_of == "rep@kinaxis.com"
        assert "@" in q.asked_of


def test_ask_person_source_is_unavailable_with_no_party_and_says_why():
    opts = seek.enumerate_sources(_gap("stale_evidence"), parties=[])
    ask = [o for o in opts if o.kind == "ask_person"]
    assert len(ask) == 1
    assert ask[0].available is False
    assert ask[0].target is None
    assert "nobody to ask" in ask[0].why_unavailable
    assert "not guess" in ask[0].why_unavailable


# ------------------------------------------------- RULE 2: never rank the sources

def test_sources_carry_an_availability_flag_not_a_score():
    """An unreachable source must still be NAMED - a human can act on what
    Jasper cannot. What must NOT appear is a rank/score/priority."""
    opts = seek.enumerate_sources(_gap("unresolved_reference", "PR9"), parties=EXTERNAL)
    assert len(opts) >= 2
    assert any(o.available for o in opts)
    assert any(not o.available for o in opts)
    for o in opts:
        d = o.as_dict()
        assert set(d) == {"kind", "what", "target", "available", "why_unavailable"}
        for banned in ("score", "rank", "weight", "priority", "confidence", "trust"):
            assert banned not in d, f"SourceOption gained a {banned} field - that ranks methods"


def test_unavailable_sources_always_explain_themselves():
    for kind, ref in (("unresolved_reference", "PR1"), ("stale_evidence", None),
                      ("missing_required_context", "amount")):
        for o in seek.enumerate_sources(_gap(kind, ref), parties=[]):
            if not o.available:
                assert o.why_unavailable, f"{kind}/{o.kind} unavailable with no reason"


def test_system_of_record_is_named_but_marked_unreachable():
    """Ariba/SAP is real and confirmed live via ARIA, but only through an
    interactive session - unattended code cannot reach it. Naming it anyway is
    the point of the available flag."""
    opts = seek.enumerate_sources(_gap("unresolved_reference", "PR7"), parties=EXTERNAL)
    sysopt = [o for o in opts if o.kind == "check_system"]
    assert len(sysopt) == 1
    assert sysopt[0].available is False
    assert "interactive" in sysopt[0].why_unavailable
    assert "unattended" in sysopt[0].why_unavailable


# ------------------------------------------------- RULE 3: deterministic, no LLM/writes

def test_deterministic():
    g = _gap("unresolved_reference", "PR42")
    a = [o.as_dict() for o in seek.enumerate_sources(g, parties=EXTERNAL)]
    b = [o.as_dict() for o in seek.enumerate_sources(g, parties=EXTERNAL)]
    assert a == b
    assert ([q.as_dict() for q in seek.generate_questions([g], parties=EXTERNAL)] ==
            [q.as_dict() for q in seek.generate_questions([g], parties=EXTERNAL)])


def test_no_model_call_and_no_writes_in_the_pure_functions():
    import inspect
    for fn in (seek.enumerate_sources, seek.generate_questions):
        src = inspect.getsource(fn)
        for banned in ("subprocess", "Popen", "claude", "INSERT INTO", "UPDATE ",
                       "DELETE FROM", "commit()"):
            assert banned not in src, f"{fn.__name__} contains {banned!r}"


# ------------------------------------------------- behaviour

def test_unrecognized_gap_kind_yields_nothing_invented():
    """An admitted blank beats a generic guess."""
    assert seek.enumerate_sources(_gap("some_future_gap_kind"), parties=EXTERNAL) == []
    assert seek.generate_questions([_gap("some_future_gap_kind")], parties=EXTERNAL) == []


def test_ingestion_fault_is_never_asked_of_a_counterparty():
    """claims_without_evidence is OUR bug. It belongs in the escalation package
    to Marc, never in an email to a supplier."""
    gaps = [_gap("claims_without_evidence")]
    assert seek.generate_questions(gaps, parties=EXTERNAL) == []
    # ...but it still enumerates a source, pointed at our own ingestion
    opts = seek.enumerate_sources(gaps[0], parties=EXTERNAL)
    assert opts and all(o.kind == "search_records" for o in opts)
    assert "ingestion" in opts[0].what


def test_questions_ask_for_a_fact_not_confirmation_of_a_guess():
    """A yes to a leading question is the recipient being agreeable, not
    evidence. No template may ask 'is this X?'."""
    gaps = [_gap(k, "PR5") for k in
            ("unresolved_reference", "stale_evidence",
             "closed_issue_with_open_claims", "missing_required_context")]
    for q in seek.generate_questions(gaps, parties=EXTERNAL, max_questions=99):
        low = q.text.lower()
        assert not low.startswith("is this")
        assert not low.startswith("is it")
        assert "confirm that this is" not in low


def test_every_question_states_what_the_answer_would_close():
    gaps = [_gap("unresolved_reference", "PR5"), _gap("missing_required_context", "amount")]
    for q in seek.generate_questions(gaps, parties=EXTERNAL):
        assert q.answer_would_close
        assert q.gap_kind


def test_ref_is_interpolated_into_both_text_and_closure():
    q = seek.generate_questions([_gap("unresolved_reference", "PR-777")],
                                parties=EXTERNAL)[0]
    assert "PR-777" in q.text
    assert "PR-777" in q.answer_would_close
    assert q.ref == "PR-777"


def test_max_questions_bounds_the_output():
    gaps = [_gap("unresolved_reference", f"PR{i}") for i in range(10)]
    assert len(seek.generate_questions(gaps, parties=EXTERNAL, max_questions=3)) == 3


def test_read_document_availability_reflects_real_staged_text():
    """Only 29 of 157 SharePoint items have locally-synced text, so this option
    must not claim availability it does not have."""
    g = _gap("missing_required_context", "amount")
    no = [o for o in seek.enumerate_sources(g, parties=EXTERNAL, staged_documents=set())
          if o.kind == "read_document"]
    assert no and no[0].available is False and "29 of" in no[0].why_unavailable
    yes = [o for o in seek.enumerate_sources(
        g, parties=EXTERNAL, staged_documents={"Kinaxis_amount_schedule.pdf"})
        if o.kind == "read_document"]
    assert yes and yes[0].available is True
