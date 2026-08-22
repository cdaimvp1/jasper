"""Tests for workgraph_entities (task #379 Phase 0).

Two jobs. First, pin entity_key's behaviour - it is the measurement instrument
that produced the "do not build Phases 1-4" finding, so if it silently changes,
that finding stops being reproducible. Second, and more important, guard the
separation that makes this module safe: entity_key is a STRONGER normalizer
than the one feeding candidate detection, and it must never reach that path.
"""
from __future__ import annotations

import workgraph_entities as ent


# ------------------------------------------------------------- entity_key --

def test_the_one_case_this_whole_module_measured():
    """'Fullstory, Inc' was the single real alias split in the live corpus -
    the comma survives normalize_company_name because that function strips a
    trailing legal-form suffix but no punctuation."""
    assert ent.entity_key("Fullstory, Inc") == "fullstory"
    assert ent.entity_key("Fullstory") == "fullstory"


def test_legal_forms_are_removed_anywhere_not_just_trailing():
    """The current normalizer anchors its suffix strip at end-of-string, so
    'Sodalis Inc Ltd' keeps a legal form. This is the documented improvement."""
    assert ent.entity_key("Sodalis Inc Ltd") == "sodalis"
    assert ent.entity_key("Deloitte Consulting LLP") == "deloitte consulting"
    assert ent.entity_key("Novo Nordisk A/S") == "novo nordisk"


def test_trailing_country_parenthetical_is_dropped():
    assert ent.entity_key("Microsoft Corp (US)") == "microsoft"
    assert ent.entity_key("Kinaxis (Canada)") == "kinaxis"


def test_leading_the_is_dropped():
    assert ent.entity_key("The Hartford") == "hartford"


def test_ampersand_survives_because_it_is_load_bearing():
    """'Johnson & Johnson' must not become 'johnson johnson' colliding with a
    hypothetical 'Johnson Johnson' - but more importantly the & is part of the
    real name and stripping it loses information."""
    assert "&" in ent.entity_key("Johnson & Johnson")


def test_case_and_unicode_are_normalized():
    assert ent.entity_key("ESKO") == ent.entity_key("Esko") == "esko"
    # NFKC folds the fullwidth form onto the ASCII one
    assert ent.entity_key("ＳＡＰ") == "sap"


def test_degenerate_input_returns_empty_never_a_guess():
    for bad in (None, "", "   ", ",,,", "Inc", "LLC", "the"):
        assert ent.entity_key(bad) == "", f"{bad!r} should yield '' not a fabricated key"


def test_entity_key_is_deterministic():
    for name in ("Kinaxis Corp.", "Fullstory, Inc", "Deloitte Consulting LLP"):
        assert ent.entity_key(name) == ent.entity_key(name)


# ------------------------------- the separation that makes this safe -------

def test_entity_key_is_not_reachable_from_the_matching_path():
    """REGRESSION GUARD, and the reason this lives in its own module.

    entity_key is deliberately more aggressive than
    workgraph_signals.normalize_company_name, which feeds the `supplier` data
    point in candidate detection - a path under the ROADMAP's standing 2-point
    grouping guardrail. If either grouping module ever imports this one, a
    stronger match key has entered the guarded path without the required
    regression-corpus before/after and live backtest."""
    import inspect
    import workgraph_projects
    import workgraph_pipeline2
    for mod in (workgraph_projects, workgraph_pipeline2):
        src = inspect.getsource(mod)
        assert "workgraph_entities" not in src, (
            f"{mod.__name__} imports workgraph_entities - a stronger normalizer "
            "has reached the guarded candidate-detection path")


def test_signals_module_does_not_import_this_one_either():
    import inspect
    import workgraph_signals
    assert "workgraph_entities" not in inspect.getsource(workgraph_signals)


def test_this_module_creates_no_tables():
    """Phase 1 (schema) was deliberately NOT built - the Phase 0 measurement
    said not to. If DDL appears here, someone resumed the build without
    re-running the measurement that argued against it."""
    import inspect
    # Strip the module docstring first: it DESCRIBES the algorithm in prose
    # ("drop a leading the", "drop a trailing parenthetical"), which a bare
    # "DROP " substring check matches. Caught by this test failing on itself.
    src = inspect.getsource(ent)
    src = src.replace(ent.__doc__ or "", "").upper()
    for ddl in ("CREATE TABLE", "ALTER TABLE", "INSERT INTO", "DELETE FROM",
                "DROP TABLE", "DROP INDEX"):
        assert ddl not in src, f"{ddl} found - this module is read-only by design"


def test_audit_is_read_only_and_says_so():
    """audit() reports wrote_anything=False as a machine-checkable claim, not
    just a docstring promise."""
    import inspect
    sig = inspect.signature(ent.audit)
    assert list(sig.parameters) == ["verbose"]
    src = inspect.getsource(ent.audit)
    assert '"wrote_anything": False' in src
    assert "commit()" not in src


def test_no_key_ever_contains_a_control_character():
    """REGRESSION GUARD for a real bug found 2026-08-21. The slashed-legal-form
    fold was written as a backreference STRING and the file ended up holding the
    literal bytes \x01\x02 instead of the escape sequences, so every folded key
    carried control characters. Every existing test still passed, because the
    punctuation strip on the following line removes control characters too - the
    right answer for the wrong reason. This asserts the property directly."""
    for name in ("Novo Nordisk A/S", "Something S/A", "Plain Co", "Fullstory, Inc"):
        k = ent.entity_key(name)
        assert all(ord(ch) >= 32 for ch in k), f"{name!r} -> {k!r} contains a control char"


def test_slashed_legal_form_folds_rather_than_deleting_the_letters():
    """The fold must join the letters ('a/s' -> 'as', which the token set then
    removes), not silently delete them - otherwise a non-legal-form slash pair
    would lose real characters."""
    assert ent.entity_key("Novo Nordisk A/S") == "novo nordisk"
    # 'ab' is a legal form too, so a name that is ONLY a slashed form is empty
    assert ent.entity_key("A/S") == ""
