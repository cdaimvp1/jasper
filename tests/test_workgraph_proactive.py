"""Regression tests for workgraph_proactive.py (task #287, Marc's "pre-
emptive action... gated by human approval" idea). config.get/set_value and
team_room.post_message are monkeypatched directly - these tests exercise
the detection/dispatch logic in isolation, not config's file I/O or
team_room's own bus mechanics (both tested elsewhere)."""
from __future__ import annotations

import json
import time

import config
import team_room
import workgraph_proactive as wp


def _enable_proactive_actions(monkeypatch, enabled=True):
    monkeypatch.setattr(config, "get", lambda *keys, default=None: enabled if keys == ("proactive_actions", "enabled") else default)


def _issue(ws_db, title="Issue", state="active"):
    return ws_db.create_issue_with_new_id(title=title, state=state, category="other")


def _inbound_raw_item(ws_db, issue_id, key, subject, entry_id=None):
    rid = ws_db.insert_raw_item(
        source="outlook_mail", stable_key=key, thread_key=key, dedupe_key=key,
        occurred_ts=time.time(), subject=subject, from_actor="a@example.com", participants_json="[]",
        entry_id=entry_id,
    )
    ws_db.link_raw_item_to_issue(rid, issue_id)
    ws_db.classify_raw_item(
        rid, item_class="ACTIONABLE-ASK", direction="inbound", direction_inferred=False,
        topic="other", topic_inferred=False, sentiment="neutral", sentiment_inferred=False,
        anomaly_flag=False, signal_type=None, pr_number=None, pr_number_base=None,
    )
    return rid


def test_disabled_by_default_is_a_noop(ws_db, monkeypatch):
    _enable_proactive_actions(monkeypatch, enabled=False)
    issue = _issue(ws_db)
    rid = _inbound_raw_item(ws_db, issue, "k1", "please review the attached contract")
    ws_db.create_attachment(entity_type="raw_item", entity_id=str(rid), kind="reference",
                             filename="SOW.pdf", stored_path="p.pdf", content_type="application/pdf",
                             size_bytes=10, sha256_hex="h1", uploaded_by="outlook_ingest")

    assert wp.check_raw_item_for_proactive_action(rid) is None
    assert ws_db.find_prepared_action_by_idempotency_key(wp._idempotency_key(rid, "review_contract")) is None


def test_contract_review_dispatches_when_attachment_and_phrase_both_match(ws_db, monkeypatch):
    _enable_proactive_actions(monkeypatch)
    monkeypatch.setattr(team_room, "post_message", lambda sender, body: {"message_id": "tr_fake1"})
    issue = _issue(ws_db)
    rid = _inbound_raw_item(ws_db, issue, "k2", "please review the attached contract")
    ws_db.create_attachment(entity_type="raw_item", entity_id=str(rid), kind="reference",
                             filename="SOW.pdf", stored_path="p.pdf", content_type="application/pdf",
                             size_bytes=10, sha256_hex="h2", uploaded_by="outlook_ingest")

    result = wp.check_raw_item_for_proactive_action(rid)

    assert result == "review_contract"
    prepared = ws_db.find_prepared_action_by_idempotency_key(wp._idempotency_key(rid, "review_contract"))
    assert prepared is not None
    # Design doc Section 11: dispatching the team_room message only confirms
    # the request reached bridge, not that the review itself ever completed -
    # "uncertain", not "succeeded".
    assert prepared["state"] == "uncertain"
    assert prepared["rationale"].startswith("Proactive:")


def test_contract_review_skipped_without_a_contract_like_attachment(ws_db, monkeypatch):
    _enable_proactive_actions(monkeypatch)
    monkeypatch.setattr(team_room, "post_message", lambda sender, body: {"message_id": "tr_fake"})
    issue = _issue(ws_db)
    rid = _inbound_raw_item(ws_db, issue, "k3", "please review the attached contract")

    assert wp.check_raw_item_for_proactive_action(rid) is None


def test_contract_review_skipped_without_matching_phrase(ws_db, monkeypatch):
    _enable_proactive_actions(monkeypatch)
    issue = _issue(ws_db)
    rid = _inbound_raw_item(ws_db, issue, "k4", "just saying hello, no ask here")
    ws_db.create_attachment(entity_type="raw_item", entity_id=str(rid), kind="reference",
                             filename="SOW.pdf", stored_path="p.pdf", content_type="application/pdf",
                             size_bytes=10, sha256_hex="h4", uploaded_by="outlook_ingest")

    assert wp.check_raw_item_for_proactive_action(rid) is None


def test_contract_review_is_idempotent_per_raw_item(ws_db, monkeypatch):
    _enable_proactive_actions(monkeypatch)
    calls = []
    monkeypatch.setattr(team_room, "post_message", lambda sender, body: calls.append(1) or {"message_id": "tr_fake"})
    issue = _issue(ws_db)
    rid = _inbound_raw_item(ws_db, issue, "k5", "please review the attached contract")
    ws_db.create_attachment(entity_type="raw_item", entity_id=str(rid), kind="reference",
                             filename="SOW.pdf", stored_path="p.pdf", content_type="application/pdf",
                             size_bytes=10, sha256_hex="h5", uploaded_by="outlook_ingest")

    wp.check_raw_item_for_proactive_action(rid)
    wp.check_raw_item_for_proactive_action(rid)

    assert len(calls) == 1


def test_status_update_draft_dispatches_and_saves_without_display(ws_db, monkeypatch):
    _enable_proactive_actions(monkeypatch)
    captured = {}
    import outlook_actions
    monkeypatch.setattr(outlook_actions, "draft_reply", lambda entry_id, **kw: captured.update(entry_id=entry_id, **kw) or {"ok": True})
    issue = _issue(ws_db)
    rid = _inbound_raw_item(ws_db, issue, "k6", "can I get a status update on this?", entry_id="entryid-XYZ")

    result = wp.check_raw_item_for_proactive_action(rid)

    assert result == "draft_status_update"
    assert captured["entry_id"] == "entryid-XYZ"
    assert captured["save_only"] is True
    assert "body" in captured
    prepared = ws_db.find_prepared_action_by_idempotency_key(wp._idempotency_key(rid, "draft_status_update"))
    assert prepared["state"] == "succeeded"


def test_status_update_skipped_without_entry_id(ws_db, monkeypatch):
    _enable_proactive_actions(monkeypatch)
    issue = _issue(ws_db)
    rid = _inbound_raw_item(ws_db, issue, "k7", "can I get a status update on this?", entry_id=None)

    assert wp.check_raw_item_for_proactive_action(rid) is None


def test_skips_when_no_issue_linked(ws_db, monkeypatch):
    """A raw_item still sitting in a raw cluster (not a promoted issue) -
    nothing confirmed to act on yet."""
    _enable_proactive_actions(monkeypatch)
    cluster = ws_db.create_cluster_with_new_id(title="Stray", category="other")
    rid = _inbound_raw_item(ws_db, cluster, "k8", "please review the attached contract")
    ws_db.create_attachment(entity_type="raw_item", entity_id=str(rid), kind="reference",
                             filename="SOW.pdf", stored_path="p.pdf", content_type="application/pdf",
                             size_bytes=10, sha256_hex="h8", uploaded_by="outlook_ingest")

    assert wp.check_raw_item_for_proactive_action(rid) is None


def test_skips_outbound_raw_items(ws_db, monkeypatch):
    _enable_proactive_actions(monkeypatch)
    issue = _issue(ws_db)
    rid = ws_db.insert_raw_item(source="outlook_mail", stable_key="k9", thread_key="k9", dedupe_key="k9",
                                 occurred_ts=time.time(), subject="please review the attached contract",
                                 from_actor="marc@example.com", participants_json="[]")
    ws_db.link_raw_item_to_issue(rid, issue)
    ws_db.classify_raw_item(rid, item_class="FYI-EVIDENCE", direction="outbound", direction_inferred=False,
                             topic="other", topic_inferred=False, sentiment="neutral", sentiment_inferred=False,
                             anomaly_flag=False, signal_type=None, pr_number=None, pr_number_base=None)
    ws_db.create_attachment(entity_type="raw_item", entity_id=str(rid), kind="reference",
                             filename="SOW.pdf", stored_path="p.pdf", content_type="application/pdf",
                             size_bytes=10, sha256_hex="h9", uploaded_by="outlook_ingest")

    assert wp.check_raw_item_for_proactive_action(rid) is None


def test_sweep_is_a_noop_when_disabled(ws_db, monkeypatch):
    _enable_proactive_actions(monkeypatch, enabled=False)

    result = wp.run_proactive_actions_sweep()

    assert result == {"enabled": False, "checked": 0, "dispatched": 0}


def test_sweep_advances_cursor_and_dispatches(ws_db, monkeypatch):
    _enable_proactive_actions(monkeypatch)
    monkeypatch.setattr(team_room, "post_message", lambda sender, body: {"message_id": "tr_fake"})
    issue = _issue(ws_db)
    rid = _inbound_raw_item(ws_db, issue, "k10", "please review the attached contract")
    ws_db.create_attachment(entity_type="raw_item", entity_id=str(rid), kind="reference",
                             filename="SOW.pdf", stored_path="p.pdf", content_type="application/pdf",
                             size_bytes=10, sha256_hex="h10", uploaded_by="outlook_ingest")

    result = wp.run_proactive_actions_sweep()

    assert result == {"enabled": True, "checked": 1, "dispatched": 1}
    assert int(ws_db.get_cursor(wp._CURSOR_SOURCE, wp._CURSOR_KEY)) == rid

    second = wp.run_proactive_actions_sweep()
    assert second == {"enabled": True, "checked": 0, "dispatched": 0}
