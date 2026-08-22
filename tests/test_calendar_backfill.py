"""Tests for ingest/calendar_backfill.py (task #413).

Pins the funnel's shape and the two guards that came directly out of the
25-event pilot, so a future batch cannot silently widen what gets staged.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "ingest"))

from ingest import calendar_backfill as cb


def _ev(subject, organizer="marc@lilly.com", attendees=None, eid=None):
    return {"id": eid or subject, "subject": subject, "organizer": organizer,
            "attendees": attendees if attendees is not None else [],
            "start": {"dateTime": "2026-05-01T09:00:00"}}


def test_non_work_filter_excludes_the_two_pilot_failures():
    """Both of these were staged in the pilot, both carried a real external
    attendee (so every party-based filter passed them), and both orphaned
    into their own project. That is the evidence for this guard."""
    assert cb._looks_like_non_work("This One's For You! Webinar: Emerging Risks in 2026")
    assert cb._looks_like_non_work("Save The Date - Tim Coleman Retirement Reception")


def test_non_work_filter_keeps_real_work():
    """Must not cost a single genuine working session."""
    for s in ("Lilly | Acceldata: Architecture & Design for Pilot",
              "Actigraph MSA",
              "Marc/ZS Monthly Connect",
              "Lilly - SAP ERP RISE & Ariba: Commercial Update",
              "Veeva MLR Control Plane - Future Thinking"):
        assert not cb._looks_like_non_work(s), s


def test_non_work_patterns_are_whole_phrases_not_bare_words():
    """"webinar:" not "webinar" - a real working session that merely mentions
    hosting one must survive. This is why the list holds phrases."""
    assert not cb._looks_like_non_work("Prep for supplier webinar we are hosting")
    assert cb._looks_like_non_work("Webinar: vendor roadmap")


def test_internal_only_event_is_never_eligible(ws_db):
    """The core lesson from #414, encoded: an event with no external party
    cannot reach 2 data points, so staging it manufactures a singleton
    project. 455 of 842 gate-passing events are in this category."""
    scan = {"events": [_ev("Internal planning sync",
                           attendees=["colleague@lilly.com"])]}
    picked, stats = cb.select(scan, limit=10)
    assert picked == []
    assert stats["no_external"] == 1


def test_personal_block_is_filtered_before_anything_else(ws_db):
    """A solo hold - organizer is the only participant. Depends on the
    scanner resolving both to SMTP; before that fix the organizer was a
    display name and the attendee an X.500 DN, so this never matched."""
    scan = {"events": [_ev("HOLD", organizer="marc@lilly.com",
                           attendees=["marc@lilly.com"])]}
    picked, stats = cb.select(scan, limit=10)
    assert picked == []
    assert stats["personal"] == 1


def test_select_is_deterministic_so_dry_run_matches_apply(ws_db):
    """A dry run must pick exactly what --apply would pick, or the preview
    is worthless.

    Takes ws_db because select() reads the fast-track supplier vocabulary via
    ws.list_data_point_values_for_definition. Without an initialized DB these
    three tests hit the wiped fallback scratch dir and died on "no such table:
    data_point_values" - they never passed, from the commit that added them."""
    scan = {"events": [_ev(f"Meeting {i}", attendees=[f"rep@vendor{i}.com"])
                       for i in range(6)]}
    a, _ = cb.select(scan, limit=3)
    b, _ = cb.select(scan, limit=3)
    assert [e["id"] for _, e in a] == [e["id"] for _, e in b]
