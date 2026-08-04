"""Tests for workgraph_contract_panel.py (task #50,
docs/design/CONTRACT_REVIEW_SME_PANEL.md) - the deterministic keyword
pre-filter and finding-reconciliation halves of the contract_review Pass 3
SME panel. No LLM calls anywhere in the module under test; a small fake
sme-matrix.md is injected rather than depending on the real vendored
skill file, so these tests never touch TEAM_DATA_DIR."""
import workgraph_contract_panel as wcp

_FAKE_MATRIX = """
# SME Escalation Matrix

## SME Directory

### Tax - Adam C Shields
- **Email:** shields_adam@lilly.com
- **Triggers:** tax, VAT, withholding, gross-up
- **Scope:** All tax-related contract provisions.
- **Escalation threshold:** Any modification to tax provisions - no de minimis exception

### AI/Privacy - Legal AIPC
- **Email:** Mailbox_Privacy_Contracts@lilly.com
- **Triggers:** AI, LLM, machine learning, privacy, data processing agreement
- **Scope:** All AI governance provisions.
- **Escalation threshold:** Any AI/ML involvement; any personal data processing

### InfoSec - Cyber ISS Review
- **Email:** Cyber_ISS_Review@lilly.com
- **Triggers:** security, cybersecurity, encryption, access controls
- **Scope:** Cybersecurity requirements.
- **Escalation threshold:** Any supplier with access to Lilly systems

### Subcontractor Requirements (Affirmative Action / EEO) - UNASSIGNED
- **Scope:** Human Resources subject area. No name or email assigned.

## Contract Request and Consultation Tool

| Topic | When to Use |
|---|---|
| Any provision not covered above | Novel or unusual provisions |

---
"""


def test_load_sme_directory_parses_every_real_entry():
    directory = wcp.load_sme_directory(_FAKE_MATRIX)
    names = {s["sme_name"] for s in directory}
    assert "Tax - Adam C Shields" in names
    assert "AI/Privacy - Legal AIPC" in names
    assert "InfoSec - Cyber ISS Review" in names


def test_load_sme_directory_skips_entry_with_no_triggers_line():
    """The UNASSIGNED EEO entry has no real trigger list - an honest skip,
    never an invented keyword to match on."""
    directory = wcp.load_sme_directory(_FAKE_MATRIX)
    names = {s["sme_name"] for s in directory}
    assert not any("UNASSIGNED" in n for n in names)


def test_load_sme_directory_extracts_email_and_triggers():
    directory = wcp.load_sme_directory(_FAKE_MATRIX)
    tax = next(s for s in directory if s["sme_name"].startswith("Tax"))
    assert tax["email"] == "shields_adam@lilly.com"
    assert "tax" in tax["triggers"]
    assert "gross-up" in tax["triggers"]
    assert "no de minimis exception" in tax["escalation_threshold"]


def test_identify_triggered_smes_matches_real_hit():
    directory = wcp.load_sme_directory(_FAKE_MATRIX)
    triggered = wcp.identify_triggered_smes(
        "Supplier shall handle tax withholding per Section 8.", directory)
    names = {t["sme_name"] for t in triggered}
    assert any(n.startswith("Tax") for n in names)
    assert not any(n.startswith("AI/Privacy") for n in names)
    assert not any(n.startswith("InfoSec") for n in names)


def test_identify_triggered_smes_returns_which_keywords_matched():
    directory = wcp.load_sme_directory(_FAKE_MATRIX)
    triggered = wcp.identify_triggered_smes(
        "This involves an AI model and also a data processing agreement.", directory)
    ai = next(t for t in triggered if t["sme_name"].startswith("AI/Privacy"))
    assert "ai" in ai["triggers_matched"]
    assert "data processing agreement" in ai["triggers_matched"]


def test_identify_triggered_smes_whole_word_not_substring():
    """A trigger like 'ai' must not match inside an unrelated word (e.g.
    'maintain', 'certain') - this is a real, plausible false-positive risk
    for a single-letter/short trigger, exactly the kind of bug a naive
    substring search would produce silently."""
    directory = wcp.load_sme_directory(_FAKE_MATRIX)
    triggered = wcp.identify_triggered_smes(
        "The parties shall maintain certain obligations under this contract.", directory)
    assert not any(t["sme_name"].startswith("AI/Privacy") for t in triggered)


def test_identify_triggered_smes_none_when_nothing_matches():
    directory = wcp.load_sme_directory(_FAKE_MATRIX)
    assert wcp.identify_triggered_smes("A simple non-disclosure agreement with no scope.", directory) == []


def test_identify_triggered_smes_case_insensitive():
    directory = wcp.load_sme_directory(_FAKE_MATRIX)
    triggered = wcp.identify_triggered_smes("TAX WITHHOLDING obligations apply.", directory)
    assert any(t["sme_name"].startswith("Tax") for t in triggered)


# --- reconcile_panel_findings -----------------------------------------------

def test_reconcile_passes_through_unrelated_findings_unchanged():
    findings = [
        {"owning_sme": "Tax", "clause_reference": "Sec 8", "severity": "HIGH"},
        {"owning_sme": "Payment Terms", "clause_reference": "Sec 4", "severity": "LOW"},
    ]
    out = wcp.reconcile_panel_findings(findings)
    assert len(out) == 2
    assert out[0]["related_findings"] == []
    assert out[1]["related_findings"] == []


def test_reconcile_links_two_members_flagging_same_clause():
    """sme-matrix.md's own worked example: an AI data-processing provision
    triggers both AI/Privacy and InfoSec - both findings must survive,
    linked, never one silently dropped."""
    findings = [
        {"owning_sme": "AI/Privacy", "clause_reference": "Sec 9.2", "severity": "HIGH"},
        {"owning_sme": "InfoSec", "clause_reference": "Sec 9.2", "severity": "MEDIUM"},
    ]
    out = wcp.reconcile_panel_findings(findings)
    assert len(out) == 2
    ai = next(f for f in out if f["owning_sme"] == "AI/Privacy")
    infosec = next(f for f in out if f["owning_sme"] == "InfoSec")
    assert ai["related_findings"] == ["InfoSec"]
    assert infosec["related_findings"] == ["AI/Privacy"]


def test_reconcile_dedupes_same_hard_stop_across_members():
    """Several Hard Stops are also named in a specific SME's own trigger
    list (e.g. HS-1/Sanctions), so more than one member's scan can
    legitimately produce a finding citing the SAME real Hard Stop -
    risk-scoring.md's -15 deduction must apply once, not once per member."""
    findings = [
        {"owning_sme": "Trade Sanctions", "hard_stop_id": "HS-1", "severity": "HIGH"},
        {"owning_sme": "Tax", "hard_stop_id": "HS-1", "severity": "HIGH"},
        {"owning_sme": "InfoSec", "hard_stop_id": "HS-1", "severity": "HIGH"},
    ]
    out = wcp.reconcile_panel_findings(findings)
    assert len(out) == 1
    assert out[0]["owning_sme"] == "Trade Sanctions"  # first one kept


def test_reconcile_keeps_different_hard_stops_separate():
    findings = [
        {"owning_sme": "Trade Sanctions", "hard_stop_id": "HS-1", "severity": "HIGH"},
        {"owning_sme": "Adverse Events", "hard_stop_id": "HS-4", "severity": "HIGH"},
    ]
    out = wcp.reconcile_panel_findings(findings)
    assert len(out) == 2


def test_reconcile_three_members_same_clause_all_linked_to_each_other():
    findings = [
        {"owning_sme": "AI/Privacy", "clause_reference": "Sec 9.2"},
        {"owning_sme": "InfoSec", "clause_reference": "Sec 9.2"},
        {"owning_sme": "Records Retention", "clause_reference": "Sec 9.2"},
    ]
    out = wcp.reconcile_panel_findings(findings)
    assert len(out) == 3
    for f in out:
        others = {o["owning_sme"] for o in findings if o["owning_sme"] != f["owning_sme"]}
        assert set(f["related_findings"]) == others


def test_reconcile_empty_input_returns_empty_list():
    assert wcp.reconcile_panel_findings([]) == []
