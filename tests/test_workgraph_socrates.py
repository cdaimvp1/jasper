"""Regression tests for workgraph_socrates.py's _extract_candidates (task
#22): short company names (<=6 chars) require ALL-CAPS or Title-Case to
match, closing false positives where a common English word happened to also
be a real company name ("Sap", "Reply", "H1")."""
import workgraph_socrates as wsoc


def _seed_company(ws_db, company):
    ws_db.upsert_party(id=f"p_{company.lower()}", primary_email=f"rep@{company.lower()}.com",
                        display_name="Rep", affiliation="external", affiliation_confidence="H",
                        affiliation_source="domain", company=company)


def test_lowercase_common_word_does_not_match_short_company_name(ws_db):
    _seed_company(ws_db, "Sap")
    _, company = wsoc._extract_candidates("this will really sap morale on the team")
    assert company is None


def test_titlecase_short_company_name_matches(ws_db):
    _seed_company(ws_db, "Sap")
    _, company = wsoc._extract_candidates("We're renewing our Sap contract this quarter")
    assert company == "Sap"


def test_allcaps_short_company_name_matches(ws_db):
    _seed_company(ws_db, "Sap")
    _, company = wsoc._extract_candidates("We're renewing our SAP contract this quarter")
    assert company == "Sap"


def test_reply_common_word_does_not_match(ws_db):
    _seed_company(ws_db, "Reply")
    _, company = wsoc._extract_candidates("please reply to this thread")
    assert company != "Reply"  # either None or some other real company, never a false Reply match


def test_h1_stoplist_never_matches_even_capitalized(ws_db):
    """H1 conventionally stays capitalized whether it means the company or
    'first half of the year' - explicit stoplist covers what casing alone
    can't disambiguate."""
    _seed_company(ws_db, "H1")
    _, company = wsoc._extract_candidates("our H1 2026 priorities are set")
    assert company is None


def test_longer_company_name_still_case_insensitive(ws_db):
    _seed_company(ws_db, "Databricks")
    _, company = wsoc._extract_candidates("following up on the databricks renewal")
    assert company == "Databricks"
