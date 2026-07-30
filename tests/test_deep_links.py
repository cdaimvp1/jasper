"""Regression tests for deep_links.py (task #44) - Teams chat deep links built
from a teams_chat raw_item's thread_key (== the real chat_id, confirmed
against production data: ingest/normalize.py's _process_teams_chat already
sets thread_key = chat_id verbatim, no parsing needed here)."""
import deep_links


def test_teams_chat_link_builds_documented_url_shape():
    link = deep_links.teams_chat_link("19:4d1853ce-c33b@unq.gbl.spaces")
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

    assert out[0]["deep_link"]["url"].startswith("https://teams.microsoft.com/l/chat/19%3Axyz")
    assert out[0]["deep_link"]["label"] == "Open Teams chat"


def test_attach_deep_links_non_teams_evidence_gets_no_link(ws_db):
    row_id = ws_db.insert_raw_item(
        source="outlook_mail", stable_key="conv-1", thread_key="conv-1",
        dedupe_key="dk-mail-1", occurred_ts=1_800_000_000.0, subject="Hi",
        from_actor="vendor@example.com", participants_json="[]", body_preview="hello",
    )
    evidence = [{"raw_item_id": row_id, "type": "email", "summary": "x", "ts": 1_800_000_000.0}]

    out = deep_links.attach_deep_links(evidence)

    assert out[0]["deep_link"] is None


def test_attach_deep_links_missing_raw_item_id_is_safe(ws_db):
    evidence = [{"raw_item_id": None, "type": "worker_action", "summary": "x", "ts": 1.0}]
    out = deep_links.attach_deep_links(evidence)
    assert out[0]["deep_link"] is None


def test_attach_deep_links_empty_list():
    assert deep_links.attach_deep_links([]) == []
