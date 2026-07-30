"""Regression tests for workgraph_parties.py:
- machine-signal domains never get a fake "company" name (task #22-adjacent)
- name-suffix stripping in bare-name resolution (task #30 enhancement)
"""
import workgraph_parties as wp


def test_normalize_person_name_strips_generational_suffixes():
    cases = [
        ("John Smith Jr.", "john smith"),
        ("John Smith, Jr.", "john smith"),
        ("John Smith, PhD", "john smith"),
        ("Jane Doe MD", "jane doe"),
        ("Robert Jones III", "robert jones"),
        ("Plain Name", "plain name"),
    ]
    for raw, expected in cases:
        assert wp._normalize_person_name(raw) == expected, f"{raw!r} -> expected {expected!r}"


def test_bare_name_with_suffix_resolves_to_stored_party_without_one(ws_db):
    ws_db.upsert_party(id="p1", primary_email="jsmith@acme.com", display_name="John Smith",
                        affiliation="external", affiliation_confidence="H",
                        affiliation_source="domain", company="acme")
    by_dn, by_lp = wp._build_party_indexes()
    resolved = wp._resolve_bare_name("John Smith Jr.", by_dn, by_lp)
    assert resolved is not None
    assert resolved["id"] == "p1"


def test_ambiguous_exact_match_abstains_rather_than_guessing(ws_db):
    """Two different real parties sharing a display_name must abstain, not
    fall through to a weaker guess that could resolve to a THIRD party."""
    ws_db.upsert_party(id="p1", primary_email="jsmith@acme.com", display_name="John Smith",
                        affiliation="external", affiliation_confidence="H",
                        affiliation_source="domain", company="acme")
    ws_db.upsert_party(id="p2", primary_email="jsmith@othercorp.com", display_name="John Smith",
                        affiliation="external", affiliation_confidence="H",
                        affiliation_source="domain", company="othercorp")
    by_dn, by_lp = wp._build_party_indexes()
    resolved = wp._resolve_bare_name("John Smith", by_dn, by_lp)
    assert resolved is None


def test_machine_signal_domain_never_gets_a_company_name():
    assert wp._is_machine_signal_domain("no-reply@ansmtp.ariba.com") is True
    assert wp._is_machine_signal_domain("EmailReminderService@concursolutions.com") is True
    assert wp._is_machine_signal_domain("real.person@acme.com") is False
