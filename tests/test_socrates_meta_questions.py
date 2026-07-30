"""Regression tests for workgraph_socrates.py's meta-question handler (task
#36) - a question about Jasper itself ("what can you do") used to fall
through the normal evidence pipeline and hit the generic "I don't have
grounded evidence for that yet" abstain. Fixed with a small, anchored,
deterministic pattern match (not an LLM call) returning a fixed, accurate
description of the real system.
"""
import workgraph_socrates as wsoc


def test_meta_question_gets_a_real_answer(ws_db):
    result = wsoc.answer(question="what can you do")
    assert result["outcome"] == "answered"
    assert result["confidence"] == "high"
    assert "grounded evidence" not in result["answer"]  # not the generic abstain


def test_bare_help_is_recognized(ws_db):
    result = wsoc.answer(question="help")
    assert result["outcome"] == "answered"
    assert result["depth"] == "meta"


def test_real_question_containing_help_is_not_treated_as_meta(ws_db):
    """The exact false positive confirmed during development: a real business
    question that happens to contain the word 'help' must NOT be swallowed
    by the meta-question shortcut."""
    result = wsoc.answer(question="can you help me understand the Acme escalation")
    assert result["depth"] != "meta"


def test_real_question_containing_how_do_you_work_is_not_meta():
    """Same false-positive class, different phrase."""
    matched = wsoc._META_QUESTION_RE.search("how do you work through the SAP renewal timeline")
    assert matched is None


def test_normal_business_question_unaffected(ws_db):
    result = wsoc.answer(question="what is the status of the Acme renewal")
    assert result["depth"] != "meta"
