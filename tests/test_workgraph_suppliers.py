"""Regression tests for workgraph_suppliers.py (task #75, Supplier
Relationship Dashboard). Pure aggregation over parties/issues - groups by
external party company, the same grouping key Aristotle's
match_on='supplier' rules already use."""
from __future__ import annotations

import time

import workgraph_suppliers as wsup


def _party(ws_db, party_id, company, email=None):
    ws_db.upsert_party(id=party_id, primary_email=email or f"{party_id}@example.com",
                        display_name=party_id, affiliation="external",
                        affiliation_confidence="H", affiliation_source="domain", company=company)


def test_list_suppliers_empty_when_no_external_parties(ws_db):
    assert wsup.list_suppliers() == []


def test_list_suppliers_groups_by_company(ws_db):
    _party(ws_db, "p1", "Acme")
    iid = ws_db.create_issue_with_new_id(title="Deal", state="active", category="other")
    ws_db.link_party_to_issue(iid, "p1")

    suppliers = wsup.list_suppliers()

    assert len(suppliers) == 1
    assert suppliers[0]["company"] == "Acme"
    assert suppliers[0]["open_issue_count"] == 1
    assert suppliers[0]["total_issue_count"] == 1


def test_list_suppliers_two_contacts_same_company_counted_once(ws_db):
    """The whole point of grouping by company, not by party - two different
    people at the same supplier on two different threads still roll up
    into one relationship."""
    _party(ws_db, "p1", "Acme", "a@acme.com")
    _party(ws_db, "p2", "Acme", "b@acme.com")
    i1 = ws_db.create_issue_with_new_id(title="Deal 1", state="active", category="other")
    ws_db.link_party_to_issue(i1, "p1")
    i2 = ws_db.create_issue_with_new_id(title="Deal 2", state="waiting", category="other")
    ws_db.link_party_to_issue(i2, "p2")

    suppliers = wsup.list_suppliers()

    assert len(suppliers) == 1
    assert suppliers[0]["open_issue_count"] == 2


def test_list_suppliers_counts_closed_issues_separately(ws_db):
    _party(ws_db, "p1", "Acme")
    open_id = ws_db.create_issue_with_new_id(title="Open one", state="active", category="other")
    ws_db.link_party_to_issue(open_id, "p1")
    done_id = ws_db.create_issue_with_new_id(title="Done one", state="done", category="other")
    ws_db.link_party_to_issue(done_id, "p1")

    suppliers = wsup.list_suppliers()

    assert suppliers[0]["open_issue_count"] == 1
    assert suppliers[0]["total_issue_count"] == 2


def test_list_suppliers_sums_value_from_open_issues_only(ws_db):
    _party(ws_db, "p1", "Acme")
    open_id = ws_db.create_issue_with_new_id(title="Open deal", state="active", category="other")
    ws_db.link_party_to_issue(open_id, "p1")
    rid = ws_db.insert_raw_item(source="outlook_mail", stable_key="s1", thread_key="s1", dedupe_key="s1",
                                 occurred_ts=time.time(), subject="Worth $2 million", from_actor="a@example.com",
                                 participants_json="[]")
    ws_db.link_raw_item_to_issue(rid, open_id)
    done_id = ws_db.create_issue_with_new_id(title="Closed deal", state="done", category="other")
    ws_db.link_party_to_issue(done_id, "p1")
    rid2 = ws_db.insert_raw_item(source="outlook_mail", stable_key="s2", thread_key="s2", dedupe_key="s2",
                                  occurred_ts=time.time(), subject="Worth $9 million", from_actor="a@example.com",
                                  participants_json="[]")
    ws_db.link_raw_item_to_issue(rid2, done_id)

    suppliers = wsup.list_suppliers()

    assert suppliers[0]["value_found"] == 2_000_000.0  # the done issue's $9M is excluded


def test_list_suppliers_sorted_by_open_issue_count_descending(ws_db):
    _party(ws_db, "p1", "Small Co")
    small_issue = ws_db.create_issue_with_new_id(title="One deal", state="active", category="other")
    ws_db.link_party_to_issue(small_issue, "p1")

    _party(ws_db, "p2", "Big Co")
    for i in range(3):
        iid = ws_db.create_issue_with_new_id(title=f"Deal {i}", state="active", category="other")
        ws_db.link_party_to_issue(iid, "p2")

    suppliers = wsup.list_suppliers()

    assert [s["company"] for s in suppliers] == ["Big Co", "Small Co"]


def test_list_suppliers_ignores_internal_parties(ws_db):
    ws_db.upsert_party(id="p1", primary_email="colleague@lilly.com", display_name="Colleague",
                        affiliation="internal", affiliation_confidence="H",
                        affiliation_source="domain", company=None)
    iid = ws_db.create_issue_with_new_id(title="Internal thing", state="active", category="other")
    ws_db.link_party_to_issue(iid, "p1")

    assert wsup.list_suppliers() == []


def test_supplier_detail_returns_none_for_unknown_company(ws_db):
    assert wsup.supplier_detail("Nobody Inc") is None


def test_supplier_detail_returns_full_issue_list(ws_db):
    _party(ws_db, "p1", "Acme")
    iid = ws_db.create_issue_with_new_id(title="Deal", state="active", category="other")
    ws_db.link_party_to_issue(iid, "p1")

    detail = wsup.supplier_detail("Acme")

    assert detail["company"] == "Acme"
    assert len(detail["issues"]) == 1
    assert detail["issues"][0]["id"] == iid
    assert "value_found" in detail["issues"][0]
    assert "has_hard_deadline" in detail["issues"][0]


# --- task #77: supplier precedent comparison -----------------------------

def _closed_issue(ws_db, party_id, company, opened_at, closed_at):
    iid = ws_db.create_issue_with_new_id(title=f"Closed deal via {party_id}", state="done", category="other")
    conn = ws_db._connect()
    conn.execute("UPDATE issues SET opened_at = ?, updated_at = ? WHERE id = ?", (opened_at, closed_at, iid))
    conn.close()
    _party(ws_db, party_id, company)
    ws_db.link_party_to_issue(iid, party_id)
    return iid


def test_last_closed_issue_none_when_nothing_closed(ws_db):
    _party(ws_db, "p1", "Acme")
    iid = ws_db.create_issue_with_new_id(title="Still open", state="active", category="other")
    ws_db.link_party_to_issue(iid, "p1")

    assert wsup.last_closed_issue_for_company("Acme") is None


def test_last_closed_issue_returns_most_recent(ws_db):
    now = time.time()
    older = _closed_issue(ws_db, "p1", "Acme", now - 40 * 86400, now - 30 * 86400)
    newer = _closed_issue(ws_db, "p2", "Acme", now - 10 * 86400, now - 2 * 86400)

    precedent = wsup.last_closed_issue_for_company("Acme")

    assert precedent["issue_id"] == newer
    assert 7.5 < precedent["days_to_close"] < 8.5


def test_last_closed_issue_excludes_given_issue(ws_db):
    now = time.time()
    only_closed = _closed_issue(ws_db, "p1", "Acme", now - 10 * 86400, now - 2 * 86400)

    assert wsup.last_closed_issue_for_company("Acme", exclude_issue_id=only_closed) is None


def test_attach_supplier_precedent_finds_real_company_precedent(ws_db):
    now = time.time()
    _closed_issue(ws_db, "p1", "Acme", now - 20 * 86400, now - 10 * 86400)

    open_issue_id = ws_db.create_issue_with_new_id(title="New deal", state="active", category="other")
    _party(ws_db, "p2", "Acme")
    ws_db.link_party_to_issue(open_issue_id, "p2")
    issue = ws_db.get_issue(open_issue_id)

    wsup.attach_supplier_precedent(issue)

    assert issue["supplier_precedent"] is not None
    assert issue["supplier_precedent"]["days_to_close"] == 10.0


def test_attach_supplier_precedent_none_when_no_external_company(ws_db):
    iid = ws_db.create_issue_with_new_id(title="No supplier", state="active", category="other")
    issue = ws_db.get_issue(iid)

    wsup.attach_supplier_precedent(issue)

    assert issue["supplier_precedent"] is None


def test_attach_supplier_precedent_ignores_system_sender_only_party(ws_db):
    """A party with no identified company (e.g. Ariba's own no-reply
    sender) must never be treated as "the supplier" - confirmed real
    failure mode from task #81's investigation."""
    iid = ws_db.create_issue_with_new_id(title="Ariba notice", state="active", category="other")
    ws_db.upsert_party(id="ariba", primary_email="no-reply@ansmtp.ariba.com", display_name="Ariba",
                        affiliation="external", affiliation_confidence="H",
                        affiliation_source="domain", company=None)
    ws_db.link_party_to_issue(iid, "ariba")
    issue = ws_db.get_issue(iid)

    wsup.attach_supplier_precedent(issue)

    assert issue["supplier_precedent"] is None


def test_attach_supplier_precedent_deterministic_tie_break_by_first_seen(ws_db):
    """Hardening pass #2: list_parties_for_issue has no ORDER BY, so
    picking the first match from an unordered result was non-
    deterministic when an issue has more than one identifiable external
    company. first_seen_ts ascending (earliest-known contact) is a real,
    stable tie-break."""
    now = time.time()
    earlier_closed = _closed_issue(ws_db, "p_early", "EarlierCo", now - 20 * 86400, now - 10 * 86400)
    _closed_issue(ws_db, "p_late", "LaterCo", now - 5 * 86400, now - 1 * 86400)

    iid = ws_db.create_issue_with_new_id(title="Multi-party open issue", state="active", category="other")
    ws_db.upsert_party(id="later_party", primary_email="later@later.com", display_name="later",
                        affiliation="external", affiliation_confidence="H",
                        affiliation_source="domain", company="LaterCo")
    ws_db.link_party_to_issue(iid, "later_party")
    ws_db.upsert_party(id="earlier_party", primary_email="earlier@earlier.com", display_name="earlier",
                        affiliation="external", affiliation_confidence="H",
                        affiliation_source="domain", company="EarlierCo")
    ws_db.link_party_to_issue(iid, "earlier_party")
    conn = ws_db._connect()
    conn.execute("UPDATE parties SET first_seen_ts = ? WHERE id = ?", (200.0, "later_party"))
    conn.execute("UPDATE parties SET first_seen_ts = ? WHERE id = ?", (100.0, "earlier_party"))
    conn.close()

    issue = ws_db.get_issue(iid)
    wsup.attach_supplier_precedent(issue)

    assert issue["supplier_precedent"]["issue_id"] == earlier_closed


# --- enhancement #3: Aristotle gate status + Total Recall precedent join --

def test_list_suppliers_gated_open_issue_count_zero_when_none_gated(ws_db):
    _party(ws_db, "p1", "Acme")
    iid = ws_db.create_issue_with_new_id(title="Deal", state="active", category="other")
    ws_db.link_party_to_issue(iid, "p1")

    suppliers = wsup.list_suppliers()

    assert suppliers[0]["gated_open_issue_count"] == 0


def test_list_suppliers_gated_open_issue_count_reflects_real_flag(ws_db):
    """Reuses has_unmet_prerequisite - a column workgraph_nba.recompute_all
    already maintains - rather than re-running check_prerequisites() here."""
    _party(ws_db, "p1", "Acme")
    gated = ws_db.create_issue_with_new_id(title="Sign this", state="active", category="other")
    ws_db.link_party_to_issue(gated, "p1")
    ws_db.update_issue(gated, has_unmet_prerequisite=1)
    ungated = ws_db.create_issue_with_new_id(title="Fine", state="active", category="other")
    ws_db.link_party_to_issue(ungated, "p1")

    suppliers = wsup.list_suppliers()

    assert suppliers[0]["gated_open_issue_count"] == 1
    assert suppliers[0]["open_issue_count"] == 2


def test_list_suppliers_gated_open_issue_count_excludes_closed_issues(ws_db):
    _party(ws_db, "p1", "Acme")
    closed_gated = ws_db.create_issue_with_new_id(title="Old", state="done", category="other")
    ws_db.link_party_to_issue(closed_gated, "p1")
    ws_db.update_issue(closed_gated, has_unmet_prerequisite=1)
    open_issue = ws_db.create_issue_with_new_id(title="New", state="active", category="other")
    ws_db.link_party_to_issue(open_issue, "p1")

    suppliers = wsup.list_suppliers()

    assert suppliers[0]["gated_open_issue_count"] == 0


def test_list_suppliers_precedent_none_when_no_lesson_exists(ws_db):
    _party(ws_db, "p1", "Acme")
    iid = ws_db.create_issue_with_new_id(title="Deal", state="active", category="contract")
    ws_db.link_party_to_issue(iid, "p1")

    suppliers = wsup.list_suppliers()

    assert suppliers[0]["precedent"] is None


def test_list_suppliers_precedent_surfaces_validated_lesson_for_open_category(ws_db):
    _party(ws_db, "p1", "Acme")
    iid = ws_db.create_issue_with_new_id(title="Deal", state="active", category="contract")
    ws_db.link_party_to_issue(iid, "p1")
    source = ws_db.create_issue_with_new_id(title="Source", state="done", category="contract")
    ws_db.upsert_lesson(situation_key="category:contract|company:acme", outcome="confirmed",
                         statement="Acme contracts close fast", source_issue_id=source,
                         default_trust=0.75, bump=0.1, ceiling=0.9)

    suppliers = wsup.list_suppliers()

    assert suppliers[0]["precedent"] == {"statement": "Acme contracts close fast", "confidence": "medium"}


def test_list_suppliers_precedent_ignores_low_trust_lesson(ws_db):
    """best_lesson_for_key already abstains below MIN_TRUST - confirming the
    join respects that rather than surfacing a low-confidence guess."""
    _party(ws_db, "p1", "Acme")
    iid = ws_db.create_issue_with_new_id(title="Deal", state="active", category="contract")
    ws_db.link_party_to_issue(iid, "p1")
    source = ws_db.create_issue_with_new_id(title="Source", state="done", category="contract")
    ws_db.upsert_lesson(situation_key="category:contract|company:acme", outcome="confirmed",
                         statement="Weak signal", source_issue_id=source,
                         default_trust=0.2, bump=0.1, ceiling=0.9)

    suppliers = wsup.list_suppliers()

    assert suppliers[0]["precedent"] is None


def test_attach_supplier_precedent_includes_other_gated_count(ws_db):
    """Enhancement #89: this supplier's OTHER open gated issues, not the
    issue being viewed itself."""
    _party(ws_db, "p1", "Acme")
    other_gated = ws_db.create_issue_with_new_id(title="Sign this", state="active", category="other")
    ws_db.link_party_to_issue(other_gated, "p1")
    ws_db.update_issue(other_gated, has_unmet_prerequisite=1)

    viewed_id = ws_db.create_issue_with_new_id(title="Viewing this one", state="active", category="other")
    ws_db.link_party_to_issue(viewed_id, "p1")
    issue = ws_db.get_issue(viewed_id)

    wsup.attach_supplier_precedent(issue)

    assert issue["supplier_other_gated_count"] == 1


def test_attach_supplier_precedent_other_gated_count_excludes_self(ws_db):
    """The issue being viewed must never count itself, even if it's the
    gated one."""
    _party(ws_db, "p1", "Acme")
    viewed_id = ws_db.create_issue_with_new_id(title="Sign this", state="active", category="other")
    ws_db.link_party_to_issue(viewed_id, "p1")
    ws_db.update_issue(viewed_id, has_unmet_prerequisite=1)
    issue = ws_db.get_issue(viewed_id)

    wsup.attach_supplier_precedent(issue)

    assert issue["supplier_other_gated_count"] == 0


def test_attach_supplier_precedent_other_gated_count_zero_when_no_company(ws_db):
    iid = ws_db.create_issue_with_new_id(title="No supplier", state="active", category="other")
    issue = ws_db.get_issue(iid)

    wsup.attach_supplier_precedent(issue)

    assert issue["supplier_other_gated_count"] == 0


def test_attach_supplier_precedent_includes_portfolio_value(ws_db):
    """Enhancement idea panel #3: real dollar context for this supplier's
    OTHER open issues, on the issue panel itself - previously only
    visible via the Supplier Dashboard drill-down."""
    _party(ws_db, "p1", "Acme")
    other_open = ws_db.create_issue_with_new_id(title="Big deal", state="active", category="other")
    ws_db.link_party_to_issue(other_open, "p1")
    ra = ws_db.insert_raw_item(source="outlook_mail", stable_key="pv1", thread_key="pv1", dedupe_key="pv1",
                                occurred_ts=100.0, subject="Worth $2.5 million", from_actor="a@example.com",
                                participants_json="[]")
    ws_db.link_raw_item_to_issue(ra, other_open)

    viewed_id = ws_db.create_issue_with_new_id(title="Viewing this one", state="active", category="other")
    ws_db.link_party_to_issue(viewed_id, "p1")
    issue = ws_db.get_issue(viewed_id)

    wsup.attach_supplier_precedent(issue)

    assert issue["supplier_portfolio"] == {"other_open_issue_count": 1, "other_open_value_total": 2_500_000.0}


def test_attach_supplier_precedent_portfolio_excludes_self_and_closed(ws_db):
    _party(ws_db, "p1", "Acme")
    closed = ws_db.create_issue_with_new_id(title="Done deal", state="done", category="other")
    ws_db.link_party_to_issue(closed, "p1")

    viewed_id = ws_db.create_issue_with_new_id(title="Viewing this one", state="active", category="other")
    ws_db.link_party_to_issue(viewed_id, "p1")
    issue = ws_db.get_issue(viewed_id)

    wsup.attach_supplier_precedent(issue)

    assert issue["supplier_portfolio"] == {"other_open_issue_count": 0, "other_open_value_total": 0.0}


def test_attach_supplier_precedent_portfolio_zero_when_no_company(ws_db):
    iid = ws_db.create_issue_with_new_id(title="No supplier", state="active", category="other")
    issue = ws_db.get_issue(iid)

    wsup.attach_supplier_precedent(issue)

    assert issue["supplier_portfolio"] == {"other_open_issue_count": 0, "other_open_value_total": 0.0}


def test_list_suppliers_does_not_call_get_issue_per_issue(ws_db, monkeypatch):
    """Hardening pass #3 (HIGH): list_suppliers() used to call ws.get_issue()
    once per issue across every company - measured live at 375 individual
    sqlite connections, 3-4.5s wall-clock, freezing the single-worker
    server for that whole span. Now batched via get_issues_by_ids - confirm
    the per-issue call is gone entirely, not just reduced."""
    calls = []
    real_get_issue = ws_db.get_issue
    monkeypatch.setattr(ws_db, "get_issue", lambda *a, **k: (calls.append(1), real_get_issue(*a, **k))[1])

    for n in range(3):
        company = f"Co{n}"
        _party(ws_db, f"p{n}", company)
        for m in range(3):
            iid = ws_db.create_issue_with_new_id(title=f"Deal {n}-{m}", state="active", category="other")
            ws_db.link_party_to_issue(iid, f"p{n}")

    suppliers = wsup.list_suppliers()

    assert len(suppliers) == 3
    assert calls == [], "get_issue() must not be called per-issue - use the batched get_issues_by_ids instead"


def test_list_suppliers_value_and_counts_correct_after_batching(ws_db):
    """Correctness check alongside the query-count fix above - batching must
    not change the actual numbers."""
    _party(ws_db, "p1", "Acme")
    open_id = ws_db.create_issue_with_new_id(title="Open deal", state="active", category="other")
    ws_db.link_party_to_issue(open_id, "p1")
    rid = ws_db.insert_raw_item(source="outlook_mail", stable_key="lb1", thread_key="lb1", dedupe_key="lb1",
                                 occurred_ts=time.time(), subject="Worth $2 million", from_actor="a@example.com",
                                 participants_json="[]")
    ws_db.link_raw_item_to_issue(rid, open_id)
    done_id = ws_db.create_issue_with_new_id(title="Closed deal", state="done", category="other")
    ws_db.link_party_to_issue(done_id, "p1")

    suppliers = wsup.list_suppliers()

    assert suppliers[0]["open_issue_count"] == 1
    assert suppliers[0]["total_issue_count"] == 2
    assert suppliers[0]["value_found"] == 2_000_000.0


def test_supplier_detail_includes_gated_count_and_precedent(ws_db):
    _party(ws_db, "p1", "Acme")
    gated = ws_db.create_issue_with_new_id(title="Sign this", state="active", category="contract")
    ws_db.link_party_to_issue(gated, "p1")
    ws_db.update_issue(gated, has_unmet_prerequisite=1)
    source = ws_db.create_issue_with_new_id(title="Source", state="done", category="contract")
    ws_db.upsert_lesson(situation_key="category:contract|company:acme", outcome="confirmed",
                         statement="Acme contracts close fast", source_issue_id=source,
                         default_trust=0.75, bump=0.1, ceiling=0.9)

    detail = wsup.supplier_detail("Acme")

    assert detail["gated_open_issue_count"] == 1
    assert detail["precedent"]["statement"] == "Acme contracts close fast"
