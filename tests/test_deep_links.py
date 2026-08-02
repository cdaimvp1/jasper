"""Regression tests for deep_links.py - Teams chat deep links (task #44,
built from a teams_chat raw_item's thread_key == the real chat_id, confirmed
against production data: ingest/normalize.py's _process_teams_chat already
sets thread_key = chat_id verbatim, no parsing needed here), open-email
(task #46), draft-reply (task #47), and draft-forward (task #16) actions
for outlook_mail rows. evidence["deep_links"] is a LIST (task #47's
refactor) since a single email can carry more than one action (open +
draft reply + draft forward + a vendor link) at once."""
import json

import deep_links


def test_teams_chat_link_builds_documented_url_shape():
    link = deep_links.teams_chat_link("19:4d1853ce-c33b@unq.gbl.spaces")
    assert link["kind"] == "url"
    assert link["url"] == "https://teams.microsoft.com/l/chat/19%3A4d1853ce-c33b%40unq.gbl.spaces/0"
    assert link["label"] == "Open Teams chat"


def test_teams_chat_link_percent_encodes_colon_and_at():
    link = deep_links.teams_chat_link("19:abc@thread.v2")
    assert "%3A" in link["url"]  # ':' encoded
    assert "%40" in link["url"]  # '@' encoded
    assert ":" not in link["url"].split("/l/chat/")[1].split("/0")[0].replace("%3A", "")


def test_teams_chat_link_none_for_empty_chat_id():
    assert deep_links.teams_chat_link("") is None
    assert deep_links.teams_chat_link(None) is None


def test_draft_reply_action_none_for_non_mail_source():
    assert deep_links.draft_reply_action({"source": "teams_chat", "id": 1, "entry_id": "x"}) is None


def test_draft_reply_action_none_without_entry_id():
    assert deep_links.draft_reply_action({"source": "outlook_mail", "id": 1, "entry_id": None}) is None


def test_draft_reply_action_shape():
    action = deep_links.draft_reply_action({"source": "outlook_mail", "id": 42, "entry_id": "e1"})
    assert action == {"kind": "action", "endpoint": "/api/action/draft-reply",
                       "raw_item_id": 42, "label": "Draft reply"}


def test_draft_forward_action_none_for_non_mail_source():
    assert deep_links.draft_forward_action({"source": "teams_chat", "id": 1, "entry_id": "x"}) is None


def test_draft_forward_action_none_without_entry_id():
    assert deep_links.draft_forward_action({"source": "outlook_mail", "id": 1, "entry_id": None}) is None


def test_draft_forward_action_shape():
    action = deep_links.draft_forward_action({"source": "outlook_mail", "id": 42, "entry_id": "e1"})
    assert action == {"kind": "action", "endpoint": "/api/action/draft-forward",
                       "raw_item_id": 42, "label": "Draft forward"}


def test_attach_deep_links_teams_evidence_gets_link(ws_db):
    row_id = ws_db.insert_raw_item(
        source="teams_chat", stable_key="19:xyz@thread.v2:msg1", thread_key="19:xyz@thread.v2",
        dedupe_key="dk-teams-1", occurred_ts=1_800_000_000.0, subject=None,
        from_actor="colleague@example.com", participants_json="[]",
        body_preview="can you take a look",
    )
    issue_id = ws_db.create_issue_with_new_id(title="Teams issue", state="active", category="other")
    ws_db.link_raw_item_to_issue(row_id, issue_id)
    evidence = [{"raw_item_id": row_id, "type": "teams", "summary": "x", "ts": 1_800_000_000.0}]

    out = deep_links.attach_deep_links(evidence)

    assert len(out[0]["deep_links"]) == 1
    assert out[0]["deep_links"][0]["url"].startswith("https://teams.microsoft.com/l/chat/19%3Axyz")
    assert out[0]["deep_links"][0]["label"] == "Open Teams chat"


def test_attach_deep_links_mail_without_entry_id_gets_no_links(ws_db):
    """Rows ingested before task #43 (or where the PS scan couldn't read
    EntryID for some reason) have no entry_id - nothing to open/draft by, so
    no buttons rather than broken ones."""
    row_id = ws_db.insert_raw_item(
        source="outlook_mail", stable_key="conv-1", thread_key="conv-1",
        dedupe_key="dk-mail-1", occurred_ts=1_800_000_000.0, subject="Hi",
        from_actor="vendor@example.com", participants_json="[]", body_preview="hello",
    )
    evidence = [{"raw_item_id": row_id, "type": "email", "summary": "x", "ts": 1_800_000_000.0}]

    out = deep_links.attach_deep_links(evidence)

    assert out[0]["deep_links"] == []


def test_attach_deep_links_mail_with_entry_id_gets_both_actions(ws_db):
    row_id = ws_db.insert_raw_item(
        source="outlook_mail", stable_key="conv-2", thread_key="conv-2",
        dedupe_key="dk-mail-2", occurred_ts=1_800_000_000.0, subject="Hi again",
        from_actor="vendor@example.com", participants_json="[]", body_preview="hello",
        entry_id="entryid-XYZ",
    )
    evidence = [{"raw_item_id": row_id, "type": "email", "summary": "x", "ts": 1_800_000_000.0}]

    out = deep_links.attach_deep_links(evidence)

    links = out[0]["deep_links"]
    assert {l["label"] for l in links} == {"Open email", "Draft reply", "Draft forward"}
    assert all(l["kind"] == "action" and l["raw_item_id"] == row_id for l in links)


def test_attach_deep_links_mail_with_vendor_signal_gets_all_three_links(ws_db, isolated_paths):
    dest_dir = isolated_paths.DOCUMENTS_DIR / "raw_items" / "77"
    dest_dir.mkdir(parents=True, exist_ok=True)
    (dest_dir / "body.html").write_text(
        '<a href="https://na1.adobesign.com/public/esignWidget?wid=z">REVIEW AND SIGN</a>', encoding="utf-8",
    )
    conn = ws_db._connect()
    row_id = ws_db.insert_raw_item(
        source="outlook_mail", stable_key="conv-3", thread_key="conv-3",
        dedupe_key="dk-mail-3", occurred_ts=1_800_000_000.0, subject="Signature requested on X",
        from_actor="echosign@adobesign.com", participants_json="[]", body_preview="please sign",
        entry_id="entryid-VENDOR", raw_ref=json.dumps({"body_html": "raw_items/77/body.html"}),
    )
    conn.execute("UPDATE raw_items SET signal_type = ? WHERE id = ?", ("signature_requested", row_id))
    evidence = [{"raw_item_id": row_id, "type": "email", "summary": "x", "ts": 1_800_000_000.0}]

    out = deep_links.attach_deep_links(evidence)

    labels = {l["label"] for l in out[0]["deep_links"]}
    assert labels == {"Open email", "Draft reply", "Draft forward", "Open in Adobe Sign"}


def test_attach_deep_links_calendar_source_gets_no_links(ws_db):
    row_id = ws_db.insert_raw_item(
        source="calendar", stable_key="ev-1", thread_key="ev-1",
        dedupe_key="dk-cal-1", occurred_ts=1_800_000_000.0, subject="Sync",
        from_actor="organizer@example.com", participants_json="[]",
    )
    evidence = [{"raw_item_id": row_id, "type": "calendar", "summary": "x", "ts": 1_800_000_000.0}]

    out = deep_links.attach_deep_links(evidence)

    assert out[0]["deep_links"] == []


def test_attach_deep_links_missing_raw_item_id_is_safe(ws_db):
    evidence = [{"raw_item_id": None, "type": "worker_action", "summary": "x", "ts": 1.0}]
    out = deep_links.attach_deep_links(evidence)
    assert out[0]["deep_links"] == []


def test_attach_deep_links_empty_list():
    assert deep_links.attach_deep_links([]) == []


def test_attach_deep_links_attaches_real_occurred_ts(ws_db):
    row_id = ws_db.insert_raw_item(
        source="outlook_mail", stable_key="conv-9", thread_key="conv-9",
        dedupe_key="dk-mail-9", occurred_ts=1_785_000_000.0, subject="Confirm scheduling",
        from_actor="vendor@example.com", participants_json="[]", body_preview="hello",
    )
    evidence = [{"raw_item_id": row_id, "text": "an ask"}]

    out = deep_links.attach_deep_links(evidence)

    assert out[0]["occurred_ts"] == 1_785_000_000.0


def test_attach_deep_links_occurred_ts_none_when_raw_item_missing():
    evidence = [{"raw_item_id": None, "text": "an ask"}]
    out = deep_links.attach_deep_links(evidence)
    assert out[0]["occurred_ts"] is None
