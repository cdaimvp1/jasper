"""calendar_backfill.py - curated, bounded calendar staging. Task #413.

Replaces the throwaway pilot scripts with something re-runnable, because the
remaining ~226 events need the same treatment in further batches.

WHY THIS EXISTS RATHER THAN "just run the COM ingest". A raw 210-day scan
returns 1,302 events and staging all of them would be actively harmful,
measured 2026-08-21:
  1,302 scanned
    -384 personal blocks (is_personal_calendar_block)
    - 76 OOO subjects (is_ooo_subject)
  =  842 candidates
      387 carry >=1 external company
        251 name a counterparty ALREADY in the graph  <- what this stages
      455 are INTERNAL-ONLY
The 455 internal-only events have no external counterparty, so they cannot
earn a supplier point. Task #414 proved empirically what happens to an item
that cannot reach 2 data points: it becomes its own singleton project rather
than linking (33 of 33 SharePoint documents did exactly that, and had to be
rolled back). Staging them would manufacture ~455 fragments.

WHY IT IS NOT INERT. ingest/scheduled_refresh.py runs ~5x/day and its cycle
normalizes the drop-file inbox, classifies, then calls
workgraph_pipeline2.run_pipeline_for_ungrouped_items() with no limit
(default 500). Writing a drop file therefore triggers real LLM candidate
judgment UNATTENDED within hours. There is no "stage now, decide later" -
the drop file IS the trigger. Hence --limit is required and --apply is
explicit; a dry run writes nothing.

PILOT RESULT (25 events, 2026-08-21): calendar genuinely links, unlike
documents - a calendar event HAS parties, so the stakeholder point fires AND
_topic_key_for_signature's has_external gate is satisfied, which makes
subject_entity available too.

Usage:
    python calendar_backfill.py --limit 25              # dry run, reports only
    python calendar_backfill.py --limit 25 --apply      # writes the drop file
    python calendar_backfill.py --limit 25 --apply --max-per-counterparty 2
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import workgraph_store as ws
import workgraph_signals as wsig
import workgraph_parties as wpar
import workgraph_discovery as wd
from paths import DATA_DIR

#: Values that sit in dp-fasttrack-supplier but are not counterparties. Kept
#: deliberately small and explicit: these are the ones observed on real
#: calendar data. The upstream producers are being fixed under #415 (the
#: subdomain bug is already fixed, which is why "us"/"t"/"o" no longer
#: appear); this stays as a staging-time floor so a vocabulary regression
#: cannot quietly widen a backfill.
NOT_COUNTERPARTIES = {"gmail", "you", "ind", "mail", "us", "t", "o",
                      "name", "list", "qty", "executive", "strategy"}

#: Subject patterns observed to be real counterparty traffic but NOT work -
#: a vendor's marketing webinar and a colleague's retirement party both carry
#: a genuine external attendee, so every party-based filter above passes them.
#: Both were in the 25-event pilot and both orphaned into their own project
#: exactly as predicted, which is the evidence for this list:
#:   "This One's For You! Webinar: Emerging Risks in 2026"  (gartner)
#:   "Save The Date - Tim Coleman Retirement Reception"      (amazon)
#: Deliberately scoped to THIS staging tool rather than added to
#: workgraph_classify's global NOISE gate: these are judgements about what is
#: worth backfilling, not about what an item fundamentally is, and changing
#: global classification on two observations would be overreach. Kept as
#: whole-phrase patterns, not single words, so "webinar" alone cannot silently
#: drop a real working session that happens to mention one.
NOT_WORK_SUBJECT_PATTERNS = (
    "retirement reception", "retirement party", "save the date",
    "webinar:", "this one's for you",
)


def _looks_like_non_work(subject: str) -> bool:
    s = (subject or "").lower()
    return any(p in s for p in NOT_WORK_SUBJECT_PATTERNS)


def _external_companies(event: dict) -> set:
    """Normalized external company names on this event's organizer/attendees.
    Relies on classify_affiliation, so it inherits #415's registrable-domain
    fix - before that, us.dlapiper.com resolved to "us"."""
    out = set()
    for em in [event.get("organizer") or ""] + list(event.get("attendees") or []):
        if not em or "@" not in str(em):
            continue
        a = wpar.classify_affiliation(str(em))
        if a.get("affiliation") == "external" and a.get("company"):
            out.add(wsig.normalize_company_name(a["company"]))
    out.discard("")
    return out


def select(scan: dict, limit: int, max_per_counterparty: int = 2) -> tuple:
    """Returns (selected, stats). Selection is deterministic given the same
    scan, so a dry run and the subsequent --apply pick the same events."""
    known = {(r.get("value") or "").strip().lower()
             for r in ws.list_data_point_values_for_definition(wd.FASTTRACK_SUPPLIER_ID)}
    known.discard("")

    stats = {"scanned": 0, "personal": 0, "ooo": 0, "not_work": 0,
             "no_external": 0, "external_unknown": 0, "junk_only": 0,
             "eligible": 0}
    by_co = defaultdict(list)
    for e in scan.get("events") or []:
        stats["scanned"] += 1
        if wsig.is_personal_calendar_block(organizer=e.get("organizer"),
                                           participants=e.get("attendees") or []):
            stats["personal"] += 1
            continue
        if wsig.is_ooo_subject(e.get("subject") or ""):
            stats["ooo"] += 1
            continue
        if _looks_like_non_work(e.get("subject") or ""):
            stats["not_work"] += 1
            continue
        comps = _external_companies(e)
        if not comps:
            stats["no_external"] += 1
            continue
        hit = comps & known
        if not hit:
            stats["external_unknown"] += 1
            continue
        if not (hit - NOT_COUNTERPARTIES):
            stats["junk_only"] += 1
            continue
        stats["eligible"] += 1
        by_co[sorted(hit - NOT_COUNTERPARTIES)[0]].append(e)

    # Spread across counterparties. Sorting by descending group size and
    # taking a few from each avoids a batch dominated by one recurring
    # series - a first pilot attempt sorted by party count and filled up
    # with 8 near-duplicates of a single PwC meeting, which measured nothing.
    picked, seen_keys = [], set()
    for co in sorted(by_co, key=lambda c: (-len(by_co[c]), c)):
        for e in by_co[co][:max_per_counterparty]:
            k = e.get("id")
            if k in seen_keys:
                continue
            seen_keys.add(k)
            picked.append((co, e))
            if len(picked) >= limit:
                return picked, stats
    return picked, stats


def run(scan_path: str, limit: int, apply: bool = False,
        max_per_counterparty: int = 2) -> dict:
    scan = json.loads(Path(scan_path).read_text(encoding="utf-8"))
    picked, stats = select(scan, limit, max_per_counterparty)

    drop = None
    if apply and picked:
        payload = {
            "source": "calendar",
            "events": [e for _, e in picked],
            "events_count": len(picked),
            "window_start": scan.get("window_start"),
            "window_end": scan.get("window_end"),
            "note": f"#413 curated backfill batch of {len(picked)}",
        }
        inbox = DATA_DIR / "raw_ingest_inbox"
        inbox.mkdir(parents=True, exist_ok=True)
        drop = inbox / f"calendar_backfill_{int(time.time())}.json"
        drop.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    return {
        "funnel": stats,
        "selected": len(picked),
        "counterparties": sorted({co for co, _ in picked}),
        "subjects": [(e.get("subject") or "")[:60] for _, e in picked],
        "drop_file": str(drop) if drop else None,
        "applied": bool(apply),
        "warning": ("drop file written - scheduled_refresh will normalize, classify "
                    "and LLM-judge these unattended within hours" if drop else
                    "dry run, nothing written"),
    }


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--scan", default="_413_cal_scan_v2.json",
                    help="path to a scan produced by outlook_com_calendar_ingest.scan()")
    ap.add_argument("--limit", type=int, required=True,
                    help="max events to stage - REQUIRED, no default, because "
                         "staging triggers unattended LLM judgment")
    ap.add_argument("--max-per-counterparty", type=int, default=2)
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    print(json.dumps(run(a.scan, a.limit, a.apply, a.max_per_counterparty), indent=2))
