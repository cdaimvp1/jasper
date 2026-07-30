"""Regression tests for workgraph_lessons.py:
- best_lesson_for_key shared helper (task #30 enhancement, was duplicated in
  workgraph_socrates.py)
- lesson statement truncation past MAX_STATEMENT_LEN (task #21)
"""
import workgraph_lessons as wl


def _seed_confirmed_lesson(ws_db, key, statement, source_issue_id):
    ws_db.upsert_lesson(situation_key=key, outcome="confirmed", statement=statement,
                         source_issue_id=source_issue_id, default_trust=wl.DEFAULT_TRUST,
                         bump=wl.TRUST_BUMP, ceiling=wl.TRUST_CEILING)


def test_best_lesson_for_key_finds_confirmed_lesson(ws_db):
    iid = ws_db.create_issue_with_new_id(title="Source", state="active", category="renewal")
    key = "category:renewal|company:acme"
    _seed_confirmed_lesson(ws_db, key, "Acme renewals go smoothly", iid)
    best = wl.best_lesson_for_key(key)
    assert best is not None
    assert best["statement"] == "Acme renewals go smoothly"


def test_best_lesson_for_key_abstains_if_source_issue_deleted(ws_db):
    """Re-validates the cited source issue still exists - a dangling
    reference must be treated as absent, never applied."""
    key = "category:renewal|company:ghost"
    _seed_confirmed_lesson(ws_db, key, "stale lesson", "issue-does-not-exist")
    assert wl.best_lesson_for_key(key) is None


def test_find_matching_lesson_delegates_to_shared_helper(ws_db):
    """The issue-based read path (used by NBA scoring) must find the exact
    same lesson the key-based path (used by Socrates) does - both go through
    the one shared helper now, so they cannot silently diverge."""
    iid = ws_db.create_issue_with_new_id(title="Source", state="active", category="renewal")
    ws_db.upsert_party(id="p1", primary_email="rep@acme.com", display_name="Acme Rep",
                        affiliation="external", affiliation_confidence="H",
                        affiliation_source="domain", company="acme")
    ws_db.link_party_to_issue(iid, "p1")
    key = "category:renewal|company:acme"
    _seed_confirmed_lesson(ws_db, key, "Acme renewals go smoothly", iid)

    issue = ws_db.get_issue(iid)
    matched = wl.find_matching_lesson(issue)
    assert matched is not None
    assert matched["statement"] == "Acme renewals go smoothly"


def test_record_confirmed_truncates_long_statement(ws_db):
    """Fixed 2026-07-29: an unbounded external-party company name (e.g. a long
    legal entity name) used to push the templated statement over
    MAX_STATEMENT_LEN, which validate_lesson_write rejects on EVERY call
    (not just the first) - silently breaking trust-score updates for that
    situation_key forever, since this path also runs on repeat confirms."""
    iid = ws_db.create_issue_with_new_id(title="Source", state="active", category="renewal")
    long_company = "X" * 500
    ws_db.upsert_party(id="p1", primary_email="rep@longcorp.com", display_name="Rep",
                        affiliation="external", affiliation_confidence="H",
                        affiliation_source="domain", company=long_company)
    ws_db.link_party_to_issue(iid, "p1")

    result = wl.record_confirmed_or_rejected(issue_id_a=iid, status="confirmed")
    assert result is not None, "record_confirmed_or_rejected should have written a lesson, not silently returned None"
    assert len(result["statement"]) <= wl.MAX_STATEMENT_LEN

    # and critically: a SECOND confirm for the same situation must ALSO
    # succeed (this is the actual bug - it used to fail on every repeat too)
    result2 = wl.record_confirmed_or_rejected(issue_id_a=iid, status="confirmed")
    assert result2 is not None
    assert result2["hit_count"] == 2


def test_situation_key_for_issue_deterministic_tie_break_by_first_seen(ws_db):
    """Fixed 2026-07-30 (same real bug hardening pass #2 found twice
    elsewhere - workgraph_projects._project_name_for and
    workgraph_suppliers.attach_supplier_precedent): list_parties_for_issue
    has no ORDER BY, so picking the first external company from an
    unordered result was non-deterministic when an issue has more than one.
    first_seen_ts ascending (earliest-known contact) is the real tie-break."""
    iid = ws_db.create_issue_with_new_id(title="Multi-party", state="active", category="renewal")
    ws_db.upsert_party(id="later_party", primary_email="later@laterco.com", display_name="later",
                        affiliation="external", affiliation_confidence="H",
                        affiliation_source="domain", company="laterco")
    ws_db.link_party_to_issue(iid, "later_party")
    ws_db.upsert_party(id="earlier_party", primary_email="earlier@earlierco.com", display_name="earlier",
                        affiliation="external", affiliation_confidence="H",
                        affiliation_source="domain", company="earlierco")
    ws_db.link_party_to_issue(iid, "earlier_party")
    conn = ws_db._connect()
    conn.execute("UPDATE parties SET first_seen_ts = ? WHERE id = ?", (200.0, "later_party"))
    conn.execute("UPDATE parties SET first_seen_ts = ? WHERE id = ?", (100.0, "earlier_party"))
    conn.close()

    issue = ws_db.get_issue(iid)
    key = wl.situation_key_for_issue(issue)

    assert key == "category:renewal|company:earlierco"
