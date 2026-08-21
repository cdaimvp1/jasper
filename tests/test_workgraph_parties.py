"""Regression tests for workgraph_parties.py:
- machine-signal domains never get a fake "company" name (task #22-adjacent)
- name-suffix stripping in bare-name resolution (task #30 enhancement)
"""
import workgraph_parties as wp
import workgraph_signals


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
    # Moved to workgraph_signals.py (task #53, 2026-08-02) so workgraph_
    # projects.py's grouping code could use the same combined check.
    assert workgraph_signals._is_machine_signal_domain("no-reply@ansmtp.ariba.com") is True
    assert workgraph_signals._is_machine_signal_domain("EmailReminderService@concursolutions.com") is True
    assert workgraph_signals._is_machine_signal_domain("real.person@acme.com") is False


# --- Task #415 Bug A: registrable label, not the leftmost -------------------

def test_company_from_domain_uses_registrable_label_not_subdomain():
    """The real defect: a subdomained sender yielded the SUBDOMAIN as the
    company name, and because these feed dp-fasttrack-supplier they are a
    live matching signal, not a cosmetic label. 34 work_objects shared the
    value "us" (all from us.dlapiper.com)."""
    assert wp._company_from_domain("us.dlapiper.com") == "Dlapiper"
    assert wp._company_from_domain("t.delta.com") == "Delta"
    assert wp._company_from_domain("o.delta.com") == "Delta"
    assert wp._company_from_domain("mail.anthropic.com") == "Anthropic"
    assert wp._company_from_domain("email.zs.com") == "Zs"


def test_company_from_domain_keeps_single_label_domains_intact():
    """Regression floor. "you.com" -> You and "ind.com" -> Ind are CORRECT;
    I wrongly listed both as junk when first triaging #415 purely because
    they were short. The fix must not "correct" them into anything else."""
    assert wp._company_from_domain("you.com") == "You"
    assert wp._company_from_domain("ind.com") == "Ind"
    assert wp._company_from_domain("kinaxis.com") == "Kinaxis"
    assert wp._company_from_domain("authenticx.com") == "Authenticx"


def test_company_from_domain_handles_multipart_public_suffix():
    """"example.co.uk" must not become "Co"."""
    assert wp._company_from_domain("example.co.uk") == "Example"
    assert wp._company_from_domain("foo.example.co.uk") == "Example"
