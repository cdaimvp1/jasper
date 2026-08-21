"""Regression tests for ingest/normalize.py's _process_calendar()/_calendar_series_key()
(2026-07-31, meeting-grouping design pass).

Real bug this fixes: seriesMasterId/recurrence are NEVER populated in real
captured Graph payloads (confirmed against 10 real capture files this
session) - not a rare fallback, the only case that ever happens. Every
occurrence of a recurring series was getting its own thread_key (its own
event id), so every occurrence became its own separate Issue - confirmed
against real production data: 7 separate issues for one real "DROP-IN
HOURS" series, while ANOTHER occurrence of that same series landed in a
different project than the rest.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

BODY = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BODY / "ingest"))
import normalize  # noqa: E402


def _event(**overrides):
    ev = {
        "id": "evt-1",
        "subject": "OPTIONAL: LEAH - SFA and EVAL Agreement - DROP-IN HOURS",
        "organizer": "marcia.hakala@lilly.com",
        "attendees": ["lane_marc@lilly.com"],
        "start": {"dateTime": "2026-07-13T19:00:00.0000000"},
        "end": {"dateTime": "2026-07-13T20:00:00.0000000"},
        "summary": "",
        "isOrganizer": False,
    }
    ev.update(overrides)
    return ev


def test_series_key_uses_graph_series_master_id_when_present():
    ev = _event(seriesMasterId="AAMk-real-series-id")
    key, source = normalize._calendar_series_key(
        ev, ev["id"], ev["subject"], ev["organizer"], ev["start"]["dateTime"],
    )
    assert key == "graph:AAMk-real-series-id"
    assert source == "graph_series_master_id"


def test_series_key_falls_back_to_synthetic_when_series_master_id_absent():
    """Confirmed against real data: this is the ONLY case that ever
    actually happens, not a rare fallback."""
    ev = _event()
    key, source = normalize._calendar_series_key(
        ev, ev["id"], ev["subject"], ev["organizer"], ev["start"]["dateTime"],
    )
    assert source == "synthetic_calendar_series"
    assert key.startswith("synth:marcia.hakala@lilly.com|")


def test_synthetic_series_key_is_stable_across_occurrences_with_different_attendees():
    """The real DROP-IN HOURS series recurs Mon-Thu at the same time slot,
    but its attendee list is NOT stable occurrence-to-occurrence (first
    occurrence had 4 attendees, later ones had 2) - the key must not
    depend on attendees, or it would fragment one real series."""
    occurrence_1 = _event(id="evt-1", attendees=["a@lilly.com", "b@lilly.com", "c@lilly.com", "d@lilly.com"])
    occurrence_2 = _event(id="evt-2", attendees=["a@lilly.com"])
    key1, _ = normalize._calendar_series_key(
        occurrence_1, occurrence_1["id"], occurrence_1["subject"], occurrence_1["organizer"], occurrence_1["start"]["dateTime"])
    key2, _ = normalize._calendar_series_key(
        occurrence_2, occurrence_2["id"], occurrence_2["subject"], occurrence_2["organizer"], occurrence_2["start"]["dateTime"])
    assert key1 == key2


def test_synthetic_series_key_does_not_include_weekday():
    """The real series recurs Mon/Tue/Wed/Thu at the same time-of-day - a
    weekday-inclusive key would wrongly fragment it into up to 4 series."""
    monday = _event(id="evt-mon", start={"dateTime": "2026-07-13T19:00:00.0000000"})
    tuesday = _event(id="evt-tue", start={"dateTime": "2026-07-14T19:00:00.0000000"})
    key_mon, _ = normalize._calendar_series_key(
        monday, monday["id"], monday["subject"], monday["organizer"], monday["start"]["dateTime"])
    key_tue, _ = normalize._calendar_series_key(
        tuesday, tuesday["id"], tuesday["subject"], tuesday["organizer"], tuesday["start"]["dateTime"])
    assert key_mon == key_tue


def test_synthetic_series_key_distinguishes_different_meeting_series():
    """The other real bug: two GENUINELY different recurring meetings
    (drop-in hours vs. a separate weekly working session) must get
    DIFFERENT series keys, even with the same organizer/time slot."""
    drop_in = _event(id="evt-a", subject="OPTIONAL: LEAH - SFA and EVAL Agreement - DROP-IN HOURS")
    weekly = _event(id="evt-b", subject="FW: Leah x Lilly: Ai Model Weekly")
    key_a, _ = normalize._calendar_series_key(
        drop_in, drop_in["id"], drop_in["subject"], drop_in["organizer"], drop_in["start"]["dateTime"])
    key_b, _ = normalize._calendar_series_key(
        weekly, weekly["id"], weekly["subject"], weekly["organizer"], weekly["start"]["dateTime"])
    assert key_a != key_b


def test_series_key_falls_back_to_event_id_with_no_organizer():
    ev = _event(organizer="")
    key, source = normalize._calendar_series_key(ev, ev["id"], ev["subject"], "", ev["start"]["dateTime"])
    assert key == ev["id"]
    assert source == "stable_key_fallback"


def test_process_calendar_persists_thread_key_source_and_is_organizer():
    payload = {"source": "calendar", "events": [_event(isOrganizer=True)]}
    out = normalize._process_calendar(payload)
    assert len(out) == 1
    item = out[0]
    assert item["thread_key_source"] == "synthetic_calendar_series"
    assert item["is_organizer"] == 1


def test_process_calendar_is_organizer_none_when_field_absent():
    """Confirmed against real captures: isOrganizer is sometimes entirely
    absent (older capture calls) - must stay a real NULL/None, never
    silently default to 0/False."""
    ev = _event()
    del ev["isOrganizer"]
    payload = {"source": "calendar", "events": [ev]}
    out = normalize._process_calendar(payload)
    assert out[0]["is_organizer"] is None


def test_process_calendar_two_occurrences_of_same_series_share_thread_key():
    """End-to-end: the actual bug being fixed - two occurrences of the same
    real recurring series must reduce to the same thread_key."""
    occ1 = _event(id="evt-1", start={"dateTime": "2026-07-13T19:00:00.0000000"})
    occ2 = _event(id="evt-2", start={"dateTime": "2026-07-14T19:00:00.0000000"})
    payload = {"source": "calendar", "events": [occ1, occ2]}
    out = normalize._process_calendar(payload)
    assert out[0]["thread_key"] == out[1]["thread_key"]


# --- E7 (enhancement idea panel #7, 2026-08-03): richer calendar meta -----

def test_process_calendar_captures_free_search_level_fields_into_meta():
    ev = _event(location="Microsoft Teams Meeting", isCancelled=False,
                 webLink="https://email.lilly.com/owa/?itemid=abc", showAs="tentative",
                 importance="high", recurrence=None)
    out = normalize._process_calendar({"source": "calendar", "events": [ev]})
    meta = out[0]["meta"]
    assert meta["location"] == "Microsoft Teams Meeting"
    assert meta["is_cancelled"] is False
    assert meta["web_link"] == "https://email.lilly.com/owa/?itemid=abc"
    assert meta["show_as"] == "tentative"
    assert meta["importance"] == "high"
    assert meta["is_recurring"] is False


def test_process_calendar_is_recurring_true_when_recurrence_present():
    ev = _event(recurrence={"pattern": {"type": "weekly"}})
    out = normalize._process_calendar({"source": "calendar", "events": [ev]})
    assert out[0]["meta"]["is_recurring"] is True


def test_process_calendar_meta_omits_absent_fields_rather_than_nulling():
    """No isCancelled/webLink/etc at all (an old, pre-E7 drop file) - meta
    should just lack those keys, not carry explicit None values that would
    make a genuine future None indistinguishable from "never captured"."""
    out = normalize._process_calendar({"source": "calendar", "events": [_event()]})
    meta = out[0]["meta"]
    assert "location" not in meta
    assert "is_cancelled" not in meta
    assert "web_link" not in meta
    assert meta.get("is_recurring") is False  # recurrence key IS always checkable (absent -> None -> False)


def test_process_calendar_captures_lookahead_enrichment_when_present():
    ev = _event(attendees_detailed=[
        {"name": "Marc Lane", "address": "lane_marc@lilly.com", "type": "required", "responseStatus": "accepted"},
    ], full_body_html="<html><body><p>Agenda:</p><p>1. Review budget</p></body></html>")
    out = normalize._process_calendar({"source": "calendar", "events": [ev]})
    meta = out[0]["meta"]
    assert meta["attendees_detailed"][0]["responseStatus"] == "accepted"
    assert meta["full_agenda_text"] == "Agenda: 1. Review budget"


def test_process_calendar_no_enrichment_keys_when_absent():
    """Catch-up-window events are deliberately never enriched - meta must not
    fabricate attendees_detailed/full_agenda_text that were never fetched."""
    out = normalize._process_calendar({"source": "calendar", "events": [_event()]})
    meta = out[0]["meta"]
    assert "attendees_detailed" not in meta
    assert "full_agenda_text" not in meta


def test_process_file_persists_calendar_meta_as_json(ws_db, tmp_path):
    ev = _event(id="evt-meta-1", location="Teams", isCancelled=False)
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    f = inbox / "calendar_1.json"
    f.write_text(json.dumps({"source": "calendar", "events": [ev]}), encoding="utf-8")
    result = normalize.process_file(f)
    assert result["ok"] is True
    conn = ws_db._connect()
    try:
        row = conn.execute(
            "SELECT meta_json FROM raw_items WHERE source='calendar' AND stable_key='evt-meta-1'"
        ).fetchone()
    finally:
        conn.close()
    meta = json.loads(row["meta_json"])
    assert meta["location"] == "Teams"
    assert meta["is_cancelled"] is False


# --- Phase 0 fix (D5, 2026-08-03): dead-letter shaped-but-empty stubs ------

def test_claims_content_but_empty_detects_teams_stub_shape():
    """The real confirmed defect: a shaped-but-empty Teams payload with a
    count/note but no value/messages list used to parse as ok:True,
    items:0 and archive silently - 21 real messages lost with no trace."""
    payload = {"source": "teams_chat", "chat_id": "c1",
               "messages_raw": {"count": 21, "note": "Messages fetched from read_resource"}}
    reason = normalize._claims_content_but_empty("teams_chat", payload, [])
    assert reason is not None
    assert "count=21" in reason


def test_claims_content_but_empty_teams_genuinely_empty_pull_is_not_a_failure():
    payload = {"source": "teams_chat", "chat_id": "c1", "messages_raw": {"value": []}}
    reason = normalize._claims_content_but_empty("teams_chat", payload, [])
    assert reason is None


def test_claims_content_but_empty_teams_real_items_parsed_is_not_a_failure():
    payload = {"source": "teams_chat", "chat_id": "c1", "messages_raw": {"value": [{}]}}
    reason = normalize._claims_content_but_empty("teams_chat", payload, [{"source": "teams_chat"}])
    assert reason is None


def test_claims_content_but_empty_calendar_non_list_events_is_a_failure():
    payload = {"source": "calendar", "events": {"count": 5, "note": "stub"}}
    reason = normalize._claims_content_but_empty("calendar", payload, [])
    assert reason is not None


def test_claims_content_but_empty_calendar_real_empty_list_is_not_a_failure():
    payload = {"source": "calendar", "events": []}
    reason = normalize._claims_content_but_empty("calendar", payload, [])
    assert reason is None


def test_claims_content_but_empty_sharepoint_non_list_results_is_a_failure():
    payload = {"source": "sharepoint", "results": {"count": 3}}
    reason = normalize._claims_content_but_empty("sharepoint", payload, [])
    assert reason is not None


# --- Task #413: two holes in the original D5 shapes, both from REAL archived
# drop files found on disk 2026-08-20. These payloads are verbatim.

def test_claims_content_but_empty_catches_calendar_promise_under_a_different_key():
    """VERBATIM from raw_ingest_processed/calendar_1787242251.json, which was
    archived as SUCCESS today. payload.get("events") is None, so the original
    `events is not None` test returned clean - 50 real events were lost and the
    calendar cursor advanced past them."""
    payload = {
        "source": "calendar",
        "events_catchup_count": 25,
        "events_lookahead_count": 25,
        "catchup_window_start": "2026-08-12T12:17:40",
        "catchup_window_end": "now",
        "note": ("Event details truncated in this sample. Full implementation "
                 "would include all 25 events from each window."),
    }
    reason = normalize._claims_content_but_empty("calendar", payload, [])
    assert reason is not None
    assert "25" in reason


def test_claims_content_but_empty_catches_teams_prose_string_payload():
    """VERBATIM from raw_ingest_failed/teams_chat_1785791030154_1.json. The
    payload is a DESCRIPTION of the data instead of the data - the signature
    failure of an LLM-mediated capture path. The original isinstance(dict)
    test skipped a bare string entirely."""
    payload = {
        "source": "teams_chat",
        "chat_id": "19:81b57d1a@unq.gbl.spaces",
        "chat_meta": {"type": "oneOnOne", "members": ["Michael A Hartnagel", "Marc Lane"]},
        "messages_count": 21,
        "messages_raw": "21 messages fetched",
    }
    reason = normalize._claims_content_but_empty("teams_chat", payload, [])
    assert reason is not None


def test_positive_count_field_alone_is_enough_to_fail():
    """Generalized rule: any *_count > 0 with zero items parsed is a payload
    asserting content it did not deliver, whatever the source."""
    for source, key in (("calendar", "events_count"), ("sharepoint", "results_count")):
        payload = {"source": source, key: 7}
        assert normalize._claims_content_but_empty(source, payload, []) is not None


def test_zero_count_field_is_not_a_failure():
    """A genuinely empty pull may report its own zero. Must not false-positive."""
    payload = {"source": "calendar", "events": [], "events_count": 0}
    assert normalize._claims_content_but_empty("calendar", payload, []) is None


def test_missing_expected_list_key_is_a_failure_but_present_empty_is_not():
    assert normalize._claims_content_but_empty("sharepoint", {"source": "sharepoint"}, []) is not None
    assert normalize._claims_content_but_empty("sharepoint", {"source": "sharepoint", "results": []}, []) is None
    assert normalize._claims_content_but_empty("calendar", {"source": "calendar"}, []) is not None
    assert normalize._claims_content_but_empty("calendar", {"source": "calendar", "events": []}, []) is None


def test_real_items_parsed_short_circuits_every_new_check():
    """If items came through, none of the promise checks may fire - a payload
    that both delivers items AND carries a count must pass."""
    payload = {"source": "calendar", "events_catchup_count": 25, "note": "truncated"}
    assert normalize._claims_content_but_empty("calendar", payload, [{"source": "calendar"}]) is None


def test_process_file_routes_teams_stub_to_failure_not_silent_success(tmp_path):
    f = tmp_path / "stub.json"
    f.write_text(json.dumps({"source": "teams_chat", "chat_id": "c1",
                              "messages_raw": {"count": 21, "note": "Messages fetched from read_resource"}}),
                 encoding="utf-8")
    result = normalize.process_file(f)
    assert result["ok"] is False
    assert "count=21" in result["error"]


def test_run_routes_stub_file_to_failed_dir_and_creates_alert(ws_db, tmp_path, monkeypatch):
    inbox = tmp_path / "inbox"
    processed = tmp_path / "processed"
    failed = tmp_path / "failed"
    monkeypatch.setattr(normalize, "INBOX_DIR", inbox)
    monkeypatch.setattr(normalize, "PROCESSED_DIR", processed)
    monkeypatch.setattr(normalize, "FAILED_DIR", failed)
    inbox.mkdir(parents=True)
    (inbox / "stub.json").write_text(
        json.dumps({"source": "teams_chat", "chat_id": "c1",
                    "messages_raw": {"count": 21, "note": "Messages fetched from read_resource"}}),
        encoding="utf-8",
    )

    results = normalize.run()

    assert results[0]["ok"] is False
    assert (failed / "stub.json").exists()
    assert not (processed / "stub.json").exists()
    alerts = ws_db.list_alerts()
    assert any(a["kind"] == "anomaly" and "stub.json" in a["summary"] for a in alerts)


def test_run_processes_real_payload_and_leaves_empty_pull_as_success(ws_db, tmp_path, monkeypatch):
    """A genuinely empty pull (events: []) must still archive as ok:True,
    not get dead-lettered alongside real failures."""
    inbox = tmp_path / "inbox"
    processed = tmp_path / "processed"
    failed = tmp_path / "failed"
    monkeypatch.setattr(normalize, "INBOX_DIR", inbox)
    monkeypatch.setattr(normalize, "PROCESSED_DIR", processed)
    monkeypatch.setattr(normalize, "FAILED_DIR", failed)
    inbox.mkdir(parents=True)
    (inbox / "empty.json").write_text(json.dumps({"source": "calendar", "events": []}), encoding="utf-8")

    results = normalize.run()

    assert results[0]["ok"] is True
    assert (processed / "empty.json").exists()
    assert not (failed / "empty.json").exists()
    assert ws_db.list_alerts() == []


# --- Identity fix (D18, 2026-08-03): SharePoint container = item, not folder --

def test_process_sharepoint_thread_key_is_item_identity_not_folder():
    """The real bug: two distinct documents in the same library/folder used
    to share one thread_key (the folder path), over-collapsing distinct
    artifacts into one container. Each item is now its own container."""
    payload = {"source": "sharepoint", "results": [
        {"id": "item1", "driveId": "drive1", "name": "a.xlsx",
         "webUrl": "https://collab.lilly.com/sites/Foo/Shared Documents/a.xlsx"},
        {"id": "item2", "driveId": "drive1", "name": "b.xlsx",
         "webUrl": "https://collab.lilly.com/sites/Foo/Shared Documents/b.xlsx"},
    ]}
    out = normalize._process_sharepoint(payload)
    assert out[0]["thread_key"] != out[1]["thread_key"]
    assert out[0]["thread_key"] == out[0]["stable_key"] == "drive1:item1"
    assert out[1]["thread_key"] == out[1]["stable_key"] == "drive1:item2"


def test_process_sharepoint_persists_web_url_in_meta():
    """Task #414: webUrl used to be read and discarded, leaving meta_json
    NULL - which is why every SharePoint item reached classify with no
    signal of any kind. thread_key stays item identity (D18 above); the path
    is carried as SIGNAL only."""
    payload = {"source": "sharepoint", "results": [
        {"id": "item1", "driveId": "drive1", "name": "Sodalis_SOW.docx",
         "webUrl": "https://collab.lilly.com/sites/Foo/General/Sodalis/Sodalis_SOW.docx"},
    ]}
    out = normalize._process_sharepoint(payload)
    assert out[0]["meta"]["web_url"] == (
        "https://collab.lilly.com/sites/Foo/General/Sodalis/Sodalis_SOW.docx")
    assert out[0]["meta"]["drive_id"] == "drive1"
    assert out[0]["thread_key"] == "drive1:item1"  # D18 unchanged


def test_process_sharepoint_meta_is_none_when_no_web_url():
    """No fabricated meta - a result without webUrl or driveId carries none,
    matching the normalizer's convention elsewhere."""
    payload = {"source": "sharepoint", "results": [{"id": "i", "name": "a.xlsx"}]}
    assert normalize._process_sharepoint(payload)[0]["meta"] is None


def test_process_calendar_recurring_occurrences_get_distinct_identity():
    """Task #414: with IncludeRecurrences on, Outlook returns the SERIES
    MASTER's EntryID for every occurrence. Measured on a real 210-day scan:
    1,302 events carried only 791 distinct ids, one series contributing 160
    under a single id. stable_key must be per-OCCURRENCE or a year of a daily
    meeting collapses into one identity."""
    ev = lambda start: {"id": "ENTRY1", "subject": "HOLD", "organizer": "a@b.com",
                        "attendees": [], "start": {"dateTime": start},
                        "recurrence": {"isRecurring": True}}
    out = normalize._process_calendar(
        {"source": "calendar", "events": [ev("2026-01-23T14:00:00"), ev("2026-01-26T14:00:00")]})
    assert out[0]["stable_key"] != out[1]["stable_key"]
    assert out[0]["stable_key"] == "ENTRY1:2026-01-23T14:00:00"
    # ...but they remain ONE series for grouping purposes.
    assert out[0]["thread_key"] == out[1]["thread_key"]


def test_process_calendar_one_off_key_shape_is_unchanged():
    """Scoped deliberately: a non-recurring appointment's id is already
    unique, so its key shape stays exactly as it was before #414. Every
    calendar row already in the DB relies on that."""
    out = normalize._process_calendar({"source": "calendar", "events": [
        {"id": "ENTRY2", "subject": "One off", "organizer": "a@b.com", "attendees": [],
         "start": {"dateTime": "2026-02-01T09:00:00"}, "recurrence": None},
    ]})
    assert out[0]["stable_key"] == "ENTRY2"


def test_process_calendar_recurring_without_start_falls_back_to_id():
    """No fabricated identity: a recurring event missing a start cannot be
    keyed per-occurrence, so it keeps the bare id rather than inventing one."""
    out = normalize._process_calendar({"source": "calendar", "events": [
        {"id": "ENTRY3", "subject": "No start", "organizer": "a@b.com",
         "attendees": [], "recurrence": {"isRecurring": True}},
    ]})
    assert out[0]["stable_key"] == "ENTRY3"
