"""Regression tests for workgraph_party_review.py (task #79). Real data
was checked before building this: every external party in this install
is already affiliation_confidence='H' (that tier is only ever 'M' for
INTERNAL parties, not a real reviewable gap), so the queue is scoped
instead to "external, no identified company, not a system sender" - a
genuinely actionable finding, not noise."""
from __future__ import annotations

import workgraph_party_review as wpr


def _party(ws_db, party_id, *, affiliation="external", company=None, source="domain", email=None):
    ws_db.upsert_party(id=party_id, primary_email=email or f"{party_id}@example.com",
                        display_name=party_id, affiliation=affiliation,
                        affiliation_confidence="H", affiliation_source=source, company=company)


def test_empty_when_no_parties(ws_db):
    assert wpr.list_parties_needing_review() == []


def test_external_party_with_no_company_needs_review(ws_db):
    _party(ws_db, "p1", company=None, source="domain")
    result = wpr.list_parties_needing_review()
    assert [p["id"] for p in result] == ["p1"]


def test_external_party_with_company_excluded(ws_db):
    _party(ws_db, "p1", company="Acme", source="domain")
    assert wpr.list_parties_needing_review() == []


def test_system_sender_with_no_company_excluded(ws_db):
    """The correct, expected state for Ariba/DocuSign/etc. - not a gap."""
    _party(ws_db, "ariba", company=None, source="system_sender", email="no-reply@ansmtp.ariba.com")
    assert wpr.list_parties_needing_review() == []


def test_internal_party_excluded_regardless_of_company(ws_db):
    _party(ws_db, "p1", affiliation="internal", company=None, source="domain")
    assert wpr.list_parties_needing_review() == []


def test_mixed_set_returns_only_the_real_gap(ws_db):
    _party(ws_db, "needs_review", company=None, source="domain", email="contact@network.lilly.com")
    _party(ws_db, "resolved", company="Acme", source="domain", email="rep@acme.com")
    _party(ws_db, "system", company=None, source="system_sender", email="no-reply@docusign.net")
    _party(ws_db, "internal", affiliation="internal", company=None, source="domain", email="colleague@lilly.com")

    result = wpr.list_parties_needing_review()

    assert [p["id"] for p in result] == ["needs_review"]
