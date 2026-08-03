# Project Deep-Dive routine — curator's seeded-search wake checklist

**What this is for:** Marc's own idea (design doc Section 8.4/10) — the connector fan-out he
already does by hand ("find everything on the Workday renewal, tell me where we are") is
retrieval-from-a-known-seed, a fundamentally easier problem than the cold-discovery problem
the grouping engine solves. This routine automates exactly that, one project at a time,
sequentially, never all at once — **never on-demand-by-click** (Marc's explicit correction:
"I don't work that way, one thing at a time"). `workgraph_deepdive.py` has already picked
the ONE project for this wake and derived its search seeds before you're ever woken — your
job is only the actual search + any resulting ingestion, real judgment work deterministic
code can't do.

**This is a SEEDED SEARCH pass, not the regular ingest sweep** (`GRAPH_INGEST_ROUTINE.md`,
relay's job). Different tools, different goal: relay lists what's recently active;
this routine searches for a *specific* project's identity across everything, including
threads the deterministic matcher never connected.

## CRITICAL HONESTY REQUIREMENT (same standard as `GRAPH_INGEST_ROUTINE.md`'s relay prompt —
a real prior failure was found there: a headless run reported a confident, detailed,
entirely fabricated success while the M365 connector's auth silently didn't carry over and
nothing was actually pulled. Design doc Section 10.2 names the same risk here explicitly and
this routine has no equally clean code-verifiable proxy for it — the honesty of your own
`note` in step 6 is the only signal against it.)

**If `chat_message_search`, `outlook_email_search`, `sharepoint_search`, or `read_resource`
is missing, unavailable, or fails for ANY reason (including an auth/permission error you
cannot resolve): STOP immediately and say so plainly in your `note` — name exactly which
tool call failed and how.** Do NOT write a `note` claiming a search happened, and do NOT
report a count of items searched/found, unless you actually, really called that tool and it
returned real data this wake. A `note` reading "searched Teams/mail/SharePoint for
<seeds>, found nothing new" when the underlying tool call never actually succeeded is a
serious violation of this routine — worse than an honest "the connector didn't respond, deep
dive did not run this wake."

## Steps, in order

1. **Get your seeds.**
   ```
   GET /api/workgraph/deep-dive/next
   ```
   Returns `{"project": {...}, "seeds": {"project_id", "name", "anchors": [{"anchor_type",
   "normalized_value"}, ...]}}`. If `project` is `null`, there is nothing eligible this wake
   (every active/waiting project was deep-dived recently, or none exist) — set your status
   idle and **stop**, nothing else to do.

2. **Check Jasper's own corpus first — free, zero API risk, do this before any live search.**
   The evidence full-text index already covers everything ingested so far:
   ```
   GET /api/workgraph/evidence-search?q=<a seed term>
   ```
   Returns raw hits (`issue_id`, `raw_item_id`, `snippet`) across the WHOLE corpus, not
   scoped to this project — cross-reference against
   `GET /api/workgraph/projects/{project_id}` (its own issue list) yourself to find a hit
   whose `issue_id` is NOT already a member: that's something already ingested but never
   connected to this project. If the deterministic matcher missed it, that's real signal
   worth a look — but still never hand-attach it yourself; either it already sits in the
   existing suggestion/hold-aside queue (task #54) waiting on a human, or it doesn't and
   that's a real gap worth a note in step 6, not something this routine resolves directly.

3. **Live search — one query per seed, using the project's own name + each real identity
   anchor (never a term you invented that isn't one of these):**
   - `outlook_email_search` with the seed term (mail).
   - `chat_message_search` with the seed term (Teams) — for any real hit, `read_resource`
     on the returned chat/message URI to get the actual content, same as
     `GRAPH_INGEST_ROUTINE.md` step 2 already does.
   - `sharepoint_search` with the seed term (documents) — same shape as
     `GRAPH_INGEST_ROUTINE.md` step 4's existing per-issue searches, just seeded by the
     project instead of one issue's title.
   - Cap at a reasonable number of queries per wake (the seed list is usually small — a
     project's name plus a handful of real anchors, not an open-ended search space). If a
     429/rate-limit is hit, stop that source for this wake only, same as relay's own rule —
     do not retry-loop, move to the next seed/source with whatever you already have.

4. **Teams and SharePoint finds go through relay's own existing ingestion path — not a new
   mechanism.** Envelope shapes `GRAPH_INGEST_ROUTINE.md` already specifies
   (`{"source": "teams_chat"|"sharepoint", ...}`), written to
   `INBOX/<source>_<unix_ts>.json` (`paths.DATA_DIR / "raw_ingest_inbox"`, never a bare
   relative path — same caution as `GRAPH_INGEST_ROUTINE.md`'s own fixed bug). Then:
   ```
   python ingest/normalize.py
   ```
   `insert_raw_item` is idempotent on `stable_key` — writing something already ingested is a
   safe no-op, not a duplicate, so don't spend effort hand-deduping against what you already
   see linked to this project; the pipeline already handles it. Whatever the deterministic
   matcher then does with it (auto-link, suggestion queue, hold-aside) is correct and
   expected — **never hand-attach a find directly to this project yourself.** The whole
   point of routing through the normal pipeline is that the existing, already-tested
   identity/grouping logic makes that call, not this routine.

   **`outlook_email_search` hits are different — do NOT try to write an `outlook_mail`
   envelope for them (confirmed live, 2026-08-03: no such source exists in
   `normalize.py`'s `_PROCESSORS` — mail never went through the Teams/Calendar/SharePoint
   drop-file path at all).** Mail ingestion is `outlook_com_ingest.py`, a separate,
   independent, comprehensive scan of Marc's real mailbox folder (local Outlook COM, not
   this M365 connector) that already runs every `scheduled_refresh.py` cycle regardless of
   Deep-Dive. A real mail hit here doesn't need (or have a way to receive) manual
   ingestion — if it's genuinely in the mailbox, the next regular mail scan picks it up on
   its own. Just note the finding in step 6's report; do not attempt to write a drop file
   for it.

5. **Emit a bus event** if anything new was actually written this wake (skip if nothing was
   found — no reason to wake anyone over an empty result):
   ```python
   import bus
   bus.emit_event(source="curator", kind="ingest.raw_batch_ready", actor="curator")
   ```

6. **Report completion — the one deterministic, code-verifiable act of this whole routine:**
   ```
   POST /api/workgraph/projects/{project_id}/deep_dive_complete
   {"note": "<short, honest account of what was actually searched and found>"}
   ```
   Examples of honest notes: `"searched mail/Teams/SharePoint for 'Workday HCM Renewal',
   PR1193376, workday — found 2 new mail threads, both auto-linked"`, or `"searched all three
   sources for the project's seeds, found nothing new"` (a completely normal, expected
   outcome — recall is bounded by what the connector can retrieve, per design doc 10.3), or
   (per the honesty requirement above) `"chat_message_search failed with an auth error —
   could not search Teams this wake"`. This call is what marks `last_deep_dive_ts` — never
   skip it, even on a "found nothing" wake, or this project will look never-deep-dived and
   get picked again immediately next wake instead of rotating fairly.

7. **Stop.** Nothing else this wake — no synthesis, no classification beyond what
   `normalize.py`/the existing pipeline already does automatically, no team_room posts
   beyond what this routine itself calls for.
