"""Regression tests for workgraph_socrates.py's _extract_candidates (task
#22): short company names (<=6 chars) require ALL-CAPS or Title-Case to
match, closing false positives where a common English word happened to also
be a real company name ("Sap", "Reply", "H1")."""
import workgraph_lessons
import workgraph_socrates as wsoc


def _isolate_config(monkeypatch, tmp_path):
    """Same isolation pattern as test_workgraph_projects.py's own helper -
    config.SETTINGS_PATH is bound at import time, not per-test."""
    import config
    monkeypatch.setattr(config, "SETTINGS_PATH", tmp_path / "settings.json")
    monkeypatch.setattr(config, "_cache", {})
    monkeypatch.setattr(config, "_cache_mtime", 0.0)
    return config


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


def test_broad_research_tier_searches_beyond_default_200_limit(ws_db, monkeypatch):
    """Fixed 2026-07-30 (adversarial review round #2): ws.list_issues'
    200-row default silently capped broad-research - the tier specifically
    meant to widen the search when narrower tiers find nothing - on the
    real, larger (221-open-issue) dataset. Same missing-limit=10000 pattern
    already fixed 3x elsewhere this session."""
    seen_limits = []
    real_list_issues = ws_db.list_issues

    def spy(*args, **kwargs):
        seen_limits.append(kwargs.get("limit"))
        return real_list_issues(*args, **kwargs)

    monkeypatch.setattr(ws_db, "list_issues", spy)

    wsoc.answer(question="something nobody has ever asked before", explicit_depth="deep")

    assert seen_limits and all((limit or 0) > 200 for limit in seen_limits)


# --- Phase 0 fix (D11, 2026-08-03): lessons cross-engine leakage gate ------

def test_recall_evidence_disabled_by_default(ws_db):
    """workgraph_lessons is entirely a grouping-correction store - it must
    not satisfy Socrates recall unless the cross-engine flag is explicitly
    on, even when a real matching lesson exists."""
    workgraph_lessons.record_lesson(
        situation_key_val="category:rfp-sourcing|company:acme",
        statement="Acme RFPs of this shape usually confirm.",
        outcome="confirmed", source_issue_id=ws_db.create_issue_with_new_id(
            title="X", state="active", category="rfp-sourcing"),
    )
    ev, prov = wsoc._recall_evidence(None, "rfp-sourcing", "acme")
    assert ev["band"] == "none"
    assert "disabled" in ev["detail"]
    assert prov == []


def test_recall_evidence_uses_real_lesson_when_flag_enabled(ws_db, monkeypatch, tmp_path):
    config = _isolate_config(monkeypatch, tmp_path)
    config.set_value(True, "grouping", "legacy_lessons_cross_engine_enabled")

    workgraph_lessons.record_lesson(
        situation_key_val="category:rfp-sourcing|company:acme",
        statement="Acme RFPs of this shape usually confirm.",
        outcome="confirmed", source_issue_id=ws_db.create_issue_with_new_id(
            title="X", state="active", category="rfp-sourcing"),
    )
    ev, prov = wsoc._recall_evidence(None, "rfp-sourcing", "acme")
    assert ev["band"] != "none"
    assert prov and prov[0].startswith("recall:")


def test_answer_uses_real_grounded_detail_not_a_generic_template(ws_db, monkeypatch, tmp_path):
    """D14 fix (2026-08-03): answer() used to return a fixed 'Grounded
    evidence found via {tier}...' sentence on any cleared tier, regardless
    of what the retrieved evidence's own `detail` said. It must now surface
    the real content, hedged by the tier's own confidence band."""
    config = _isolate_config(monkeypatch, tmp_path)
    config.set_value(True, "grouping", "legacy_lessons_cross_engine_enabled")

    iid = ws_db.create_issue_with_new_id(title="X", state="active", category="rfp-sourcing")
    ws_db.upsert_party(id="p1", primary_email="rep@acme.com", display_name="Rep",
                        affiliation="external", affiliation_confidence="H",
                        affiliation_source="domain", company="Acme")
    ws_db.link_party_to_issue(iid, "p1")
    workgraph_lessons.record_lesson(
        situation_key_val="category:rfp-sourcing|company:acme",
        statement="Acme RFPs of this shape usually confirm.",
        outcome="confirmed", source_issue_id=iid,
    )

    result = wsoc.answer(question="what is the status", issue_id=iid, explicit_depth="lookup")

    assert result["outcome"] == "answered"
    assert "Acme RFPs of this shape usually confirm." in result["answer"]
    assert "Grounded evidence found via" not in result["answer"]
