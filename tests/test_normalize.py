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
