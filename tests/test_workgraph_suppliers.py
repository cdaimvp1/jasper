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
