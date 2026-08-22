"""sharepoint_local_sync.py - recover real SharePoint document text from the
local OneDrive sync, deterministically, with no credentials. Tasks #414/#417.

WHY THIS EXISTS
---------------
SharePoint raw_items carry a filename and almost nothing else. `body_preview`
is empty for them, so `text_extract.resolve_item_text()` returns a bare
filename, so every deterministic extractor downstream (reference IDs, amounts,
company mentions) sees a filename and finds nothing. That is the real reason
#414's re-link produced 33 single-item fragments and 0 merges: the candidate
GATE was taught to accept documents, but the MATCHER had no content to match
on. A document with no data points is structurally unmatchable no matter how
the gate is tuned.

Lilly will not open Graph API access (standing constraint), and the LLM relay
that used to fetch SharePoint is exactly the "LLM-mediated but shouldn't be"
category recorded in ROADMAP.md's mechanism-triage section. But a large part
of this content is already on disk: OneDrive syncs it locally. Reading it is
deterministic, free, and needs no credential at all.

WHAT IT DOES
------------
Builds a filename index over the local OneDrive roots, matches each SharePoint
raw_item's filename against it, extracts text with the existing
attachment_extract extractors, stages that text under DOCUMENTS_DIR, and points
the row's `raw_ref.body_text` at it - the SAME convention
outlook_com_ingest._absorb_body already uses for mail.

That last choice is the important one: nothing downstream needs to change.
resolve_item_text() already prefers raw_ref.body_text over body_preview, so
classify, value extraction, the data-point vocabulary, and
_matched_data_points all start seeing real document content with no edits to
any of them.

THE AMBIGUITY RULE (this is not negotiable)
-------------------------------------------
Matching is by filename, and filenames are not unique. When more than one
local file has the same name, this module ABSTAINS - it records the collision
and moves on. It does NOT pick the newest, the largest, the one in the
most-likely-looking folder, or the best fuzzy path match against the web_url.

Choosing among equally-named candidates would be Jasper deciding which
document is "the real one," i.e. authoring a resolver over evidence. That is
the one thing this codebase does not do (see docs/design/
GATES_FEDERATION_AND_MECHANISM_TRIAGE.md s0, and workgraph_ambiguity.py's
NOTE ON SOURCE TRUST). An unresolved document is a gap to surface, not a
coin to flip.

MEASURED REACH, 2026-08-21 (157 SharePoint raw_items, 133,836 local files)
-------------------------------------------------------------------------
    33   exactly one local match      -> usable
    11   multiple local matches       -> ABSTAIN (recorded, not guessed)
   113   no local match               -> nothing to do; not synced locally

Of the 33, 29 are .pdf/.docx which attachment_extract handles; 3 .json and
1 .zip have no registered extractor and are skipped rather than fed junk.
So the honest ceiling is ~29 of 157 (18%), not "SharePoint is fixed." Stated
plainly here so nobody reads this module as more than it is.

USAGE
    python ingest/sharepoint_local_sync.py                # dry run, reports only
    python ingest/sharepoint_local_sync.py --apply        # stage text + update raw_ref
    python ingest/sharepoint_local_sync.py --apply --limit 5
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import attachment_extract
import paths
import workgraph_store as ws

#: Local sync roots to index. Both the personal and the org-tenant OneDrive
#: folders exist on this machine; OneDriveCloudTemp is deliberately excluded
#: (transient upload staging, not synced content).
SYNC_ROOT_NAMES = ("OneDrive - Eli Lilly and Company", "OneDrive")

#: Text longer than this is truncated on staging. Matches the order of
#: magnitude the judgment path already budgets for a single side of a
#: comparison; a 400-page PDF's full text is not useful to a matcher and
#: would dominate every evidence packet it appears in.
MAX_STAGED_CHARS = 40_000


def _sync_roots(home: Optional[Path] = None) -> list[Path]:
    home = home or Path(os.path.expanduser("~"))
    return [home / name for name in SYNC_ROOT_NAMES if (home / name).is_dir()]


def build_filename_index(roots: Optional[list[Path]] = None) -> dict[str, list[Path]]:
    """filename.lower() -> every local path with that name.

    One walk over the sync roots. Dotted directories are skipped (OneDrive's
    own metadata folders); nothing else is filtered, because a filter here
    would be a guess about which folders "count."
    """
    roots = roots if roots is not None else _sync_roots()
    index: dict[str, list[Path]] = defaultdict(list)
    for root in roots:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            for name in filenames:
                index[name.lower()].append(Path(dirpath) / name)
    return index


def resolve_local_path(filename: str, index: dict[str, list[Path]]) -> tuple[Optional[Path], str]:
    """(path, reason). path is None whenever we decline to choose.

    Three outcomes, and only the first produces a path:
      exactly one match  -> that path, reason "unique"
      several matches    -> None, reason "ambiguous:N" - see THE AMBIGUITY RULE
      no match           -> None, reason "not_synced"
    """
    if not filename:
        return None, "no_filename"
    hits = index.get(filename.strip().lower(), [])
    if len(hits) == 1:
        return hits[0], "unique"
    if len(hits) > 1:
        return None, f"ambiguous:{len(hits)}"
    return None, "not_synced"


def _staged_rel_path(row_id: int, filename: str) -> Path:
    """Mirrors outlook_com_ingest's per-row layout so both sources stage the
    same way and DOCUMENTS_DIR stays navigable."""
    safe = "".join(ch if (ch.isalnum() or ch in "._-") else "_" for ch in filename)[:80]
    return Path("raw_items") / str(row_id) / f"sharepoint_{safe}.txt"


def process_item(row: dict, index: dict[str, list[Path]], *, apply: bool = False) -> dict:
    """One SharePoint raw_item. Never raises on a bad file - a document that
    cannot be read is a recorded skip, not a crashed sweep."""
    row_id = row["id"]
    filename = (row.get("subject") or "").strip()
    out = {"id": row_id, "filename": filename, "applied": False}

    local, reason = resolve_local_path(filename, index)
    out["reason"] = reason
    if local is None:
        return out
    out["local_path"] = str(local)

    if local.suffix.lower() not in attachment_extract._EXTRACTORS:
        out["reason"] = f"no_extractor:{local.suffix.lower()}"
        return out

    try:
        text = attachment_extract.extract_text(local)
    except Exception as e:  # a corrupt/locked file must not stop the sweep
        out["reason"] = f"extract_failed:{type(e).__name__}"
        return out

    text = (text or "").strip()
    if not text:
        # A real extractor returning nothing is a fact worth recording, not a
        # reason to write an empty body_text that would mask the filename.
        out["reason"] = "extracted_empty"
        return out

    out["chars"] = len(text)
    if len(text) > MAX_STAGED_CHARS:
        text = text[:MAX_STAGED_CHARS]
        out["truncated_to"] = MAX_STAGED_CHARS
    out["reason"] = "extracted"

    if not apply:
        return out

    rel = _staged_rel_path(row_id, filename)
    dest = paths.DOCUMENTS_DIR / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(text, encoding="utf-8")

    # Merge into any existing raw_ref rather than replacing it, so a future
    # producer's keys survive.
    try:
        ref = json.loads(row.get("raw_ref") or "{}")
        if not isinstance(ref, dict):
            ref = {}
    except (TypeError, ValueError):
        ref = {}
    ref["body_text"] = str(rel)
    ref["body_text_source"] = "local_onedrive_sync"
    ref["body_text_local_path"] = str(local)

    c = ws._connect()
    c.execute("UPDATE raw_items SET raw_ref = ? WHERE id = ?", (json.dumps(ref), row_id))
    c.commit()
    out["applied"] = True
    out["staged"] = str(rel)
    return out


def run(*, apply: bool = False, limit: Optional[int] = None) -> dict:
    index = build_filename_index()
    c = ws._connect()
    rows = [dict(r) for r in c.execute(
        "SELECT id, subject, raw_ref, meta_json FROM raw_items "
        "WHERE source = 'sharepoint' ORDER BY id")]
    if limit:
        rows = rows[:limit]

    results = [process_item(r, index, apply=apply) for r in rows]
    tally: dict[str, int] = defaultdict(int)
    for r in results:
        tally[r["reason"].split(":")[0]] += 1

    return {
        "apply": apply,
        "local_files_indexed": sum(len(v) for v in index.values()),
        "sharepoint_items": len(rows),
        "by_reason": dict(tally),
        "applied": sum(1 for r in results if r["applied"]),
        "total_chars": sum(r.get("chars", 0) for r in results),
        # Ambiguous ones are surfaced explicitly - a silently-dropped
        # collision is the failure mode this whole module is written against.
        "ambiguous": [r for r in results if r["reason"].startswith("ambiguous")],
        "results": results,
    }


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="stage extracted text and update raw_ref (default: report only)")
    ap.add_argument("--limit", type=int, default=None)
    a = ap.parse_args()
    summary = run(apply=a.apply, limit=a.limit)
    brief = {k: v for k, v in summary.items() if k != "results"}
    brief["ambiguous"] = [(r["id"], r["filename"], r["reason"]) for r in brief["ambiguous"]]
    print(json.dumps(brief, indent=2, default=str))
