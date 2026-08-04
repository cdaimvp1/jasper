# Graph ingest routine — relay's wake checklist

**What this is for:** relay's job on a wake is mechanical ingestion, never synthesis (see
`relay_role.md` / the scout archetype's charge). This routine is deliberately a checklist,
not a script — the Teams/Calendar/SharePoint MCP tools only work from inside a live Claude
Code session (the OAuth token isn't portable to a standalone process, confirmed this
session), so a real agent turn has to actually make these calls. Everything downstream of
"write the raw JSON" is pure code (`normalize.py`) — relay's own reasoning load here is zero.

**Do not classify, cluster, or interpret anything in this routine.** That's `curator`'s job.
relay's only output is raw, provenanced JSON on disk.

## Steps, in order

1. **Read cursors, and resolve the real inbox path.** For each source in `teams_chat`,
   `calendar`, `sharepoint`:
   ```python
   import workgraph_store as ws
   import paths
   cursor = ws.get_cursor(source, "default")  # None on first-ever wake
   INBOX = paths.DATA_DIR / "raw_ingest_inbox"
   ```
   **Always write drop files via `INBOX`, never via a bare relative path.** Confirmed
   2026-07-29: this routine used to say to write to the literal relative path
   `new_cohort/data/raw_ingest_inbox/...`, but relay's subprocess runs with its cwd set to
   this repo (`body/`, not the Symphony root) - that relative path silently resolved to
   `body/new_cohort/data/raw_ingest_inbox/`, a location `normalize.py` never watches. 11
   real Teams/Calendar/SharePoint captures sat there, never processed, with no error
   anywhere - `normalize.py`'s own sweep can't rescue drop files it never even scans.
   `paths.DATA_DIR` resolves through the same `TEAM_DATA_DIR`-required path every other
   part of this codebase uses (hard-fails loudly if unset, rather than guessing), so
   writing through `INBOX` can't repeat this failure mode regardless of relay's cwd.

2. **Teams chats — capped, rate-limit-aware, and now fair across wakes.**
   - Call `teams_list_chats` (paginate with `cursor` param if `moreResults` is true, but don't
     chase every page every wake) and take the **top 30** results in the list's own returned
     order as this wake's *candidate pool* — this is just a metadata list call, not the
     rate-limited operation, so a wider pool here costs nothing extra.
   - **Do NOT filter by comparing `lastUpdatedDateTime` to the stored cursor** — that field is
     when the chat was renamed or its membership changed, NOT when it last had a message (per
     the tool's own description). A stable chat (same name, same members) can have brand-new
     messages every day while this field sits frozen from months ago — filtering on it silently
     and *permanently* excludes exactly the chats you talk in most, forever, with no error
     surfaced anywhere. Confirmed live 2026-07-29: a 3-person chat (Bhaumik Oza, RJ McLaughlin,
     Marc) with a `lastUpdatedDateTime` stuck on 2026-07-15 had a message land after 5pm the
     prior day that this filter dropped silently.
   - `teams_list_chats` results are already **ordered by most recent message first** (per the
     tool's own description) — that ordering is real and still used, just not as the ONLY
     criterion any more (see the real gap this closes, below).
   - **Fixed 2026-08-01, real gap:** picking the top 5-8 by recency alone, every wake, means a
     chat that's merely a bit less chatty than a handful of very-active others never gets its
     turn — it's not that it's permanently missed (a new message there would move it back up
     the recency order eventually), it's that it waits far longer between checks than the
     chattiest few, even when a real, relevant conversation is happening in it right now.
     Confirmed live: teams_chat ingestion's own earliest-observed message was only ~5 weeks
     old, and zero of it matched a real 2.5-month negotiation's participants at all - not
     provably CAUSED by this specific selection bias alone (this file has no per-chat cursor
     today to check retroactively), but exactly the failure shape it would produce. Fix: for
     each chat in the 30-candidate pool, look up `ws.get_cursor("teams_chat", chat_id)` (a
     genuine PER-CHAT cursor - `get_cursor`/`set_cursor` already take a free-form
     `cursor_key`, so `chat_id` is a valid key today, no schema change) - `None` for a chat
     never pulled before. Re-sort the pool by that cursor **ascending, `None` first** (a
     never-yet-pulled chat always outranks one checked even a minute ago), and take the top
     5-8 from THAT order as this wake's actual pull targets - same rate-limit-respecting cap
     as before, just fair rotation instead of always favoring the same chattiest few.
   - **Cap at 5-8 chats per wake** (unchanged, already-established limit) — a 429 was hit on
     the very first attempt this session (retry-after 62s) pulling one chat's full messages;
     do not treat that as a fluke, and do not raise this specific number to compensate for
     the wider candidate pool above (that pool is list-only, free; this cap is the expensive,
     rate-limited part).
   - For each selected chat, call `read_resource` with URI
     `teams:///chats/{chatId}/messages` (confirmed working URI shape — no messageId needed
     to read a whole chat's messages).
   - **On a 429: stop pulling Teams for this wake entirely.** Do not retry-loop within one
     wake — write what you already have, update the cursor only past what was actually
     fetched, and let the next scheduled wake continue from there.
   - Write each chat's raw response verbatim (unmodified) to
     `INBOX/teams_chat_<unix_ts>.json`, one file per chat, as:
     ```json
     {"source": "teams_chat", "chat_id": "...", "chat_meta": {...from teams_list_chats...}, "messages_raw": {...from read_resource...}}
     ```
   - **Stamp the per-chat cursor for every chat actually pulled this wake** (not just the
     global one in step 5): `ws.set_cursor("teams_chat", chat_id, <this wake's run
     timestamp>)`, right after that chat's write succeeds - this is what step 2's own
     fairness re-sort reads back next wake.

3. **Calendar — two calls: a real incremental catch-up, plus a fresh lookahead.**

   **Fixed 2026-08-01, real gap found investigating a real months-long negotiation:** this
   step used to be ONE call, `afterDateTime: "3 days ago"` through `beforeDateTime: "in 14
   days"`, re-centered on "now" every single wake. Step 5 set a calendar cursor, but nothing
   ever READ it back into this query — it was purely cosmetic (only used elsewhere to
   confirm relay had run at all). Every real meeting more than 3 days in the past at every
   wake it ever existed during was gone forever the moment that 3-day trailing window moved
   past it — confirmed live: 5 real meetings spanning a 2.5-month negotiation were
   completely invisible to this pipeline for exactly this reason. Two calls now, each with a
   different, deliberate refresh rule:

   - **Catch-up call** (the real cursor, now actually used): `outlook_calendar_search` for
     `afterDateTime: <the calendar cursor read in step 1>` through `beforeDateTime: "now"`.
     - **First-ever wake only** (cursor is `None`): use `afterDateTime: "180 days ago"`
       instead — a one-time historical backfill depth, not a forever limit. 180 days is a
       judgment call, not a measured requirement; if Marc's real negotiations/relationships
       regularly run longer than that, this number should move, and a *second*, explicitly
       one-time deeper backfill (e.g. 365+ days) run once by hand is the right way to go
       further back than whatever this default is set to, rather than raising the every-
       wake default and paying for it on every single run forever.
     - This call's window only ever GROWS FORWARD (next wake's `afterDateTime` picks up
       exactly where this wake's `beforeDateTime` left off, via the cursor) — it never
       re-fetches a day it's already successfully covered.
   - **Lookahead call** (unchanged behavior from before this fix, deliberately never
     advanced past): `outlook_calendar_search` for `afterDateTime: "now"` through
     `beforeDateTime: "in 14 days"`. Always re-fetched fresh, every wake — a future meeting
     can still be rescheduled or cancelled, so this window is intentionally re-checked in
     full each time rather than ever being treated as "already covered."
   - Merge both calls' events into ONE envelope, deduped by the event's own `id` (the same
     event can legitimately appear in both calls right at the `now` boundary):
     ```json
     {"source": "calendar", "events": [ {...one event...}, {...} ]}
     ```
     Each event keeps its own fields as returned — `id`, `subject`, `organizer`, `attendees`,
     `start.dateTime`, `summary`, `seriesMasterId` if present, PLUS `location`, `isCancelled`,
     `webLink`, `showAs`, `importance`, `recurrence` (E7, 2026-08-03 — all already present in
     the search response for free, previously dropped) — nothing renamed or reshaped.
   - **Enrichment for the LOOKAHEAD events only** (E7): the search response's `attendees` is
     just a flat list of email strings and its `summary` is truncated — real per-attendee
     accept/decline/tentative status and the FULL agenda text only come from a `read_resource`
     call on `calendar:///events/{id}`, confirmed live this session (returns `attendees: [{name,
     address, type, responseStatus}, ...]` and `body.content`, full HTML). That's one extra
     Graph call per event, so only run it for the lookahead window's events (the same small,
     real, near-term set the routine already treats specially, matching the SharePoint-search
     cost discipline elsewhere in this file) — never for the catch-up window's potentially-large
     historical backlog. For each lookahead event, add `attendees_detailed` (the read_resource
     response's own `attendees` array, verbatim) and `full_body_html` (its `body.content`,
     verbatim) onto that same event object before writing.
   - Write to `INBOX/calendar_<unix_ts>.json`.

4. **SharePoint — enabled 2026-07-28, query derived from open issues (Marc's choice).**
   - Check `workgraph_store.get_cursor("sharepoint", "enabled") == "1"` before doing anything
     here — respect the flag either way, don't assume it's on.
   - Query scope: pull the top 5 open issues by `priority_score`
     (`workgraph_store.list_issues(states=["active","waiting","blocked"], limit=5)`), and run
     one `sharepoint_search` call per issue using that issue's `title` (stripped of any
     `Re:`/`Fwd:` prefix) as the query string. **Never invent a search term not drawn from an
     actual issue title** — this keeps the query scope self-updating as your work changes,
     with nothing fixed to maintain. Cap at 5 searches per wake (matches the Teams pacing
     discipline — SharePoint search is lower-volume than Teams but there's no reason to burn
     more calls than the issue list actually warrants).
   - Wrap the returned documents (each keeping its own fields — `id`, `driveId`, `webUrl`,
     `lastModifiedDateTime`, `name`, `summary`) into:
     ```json
     {"source": "sharepoint", "results": [ {...one document...}, {...} ]}
     ```
   - Write to `INBOX/sharepoint_<unix_ts>.json`.

5. **Update cursors** — only forward, only past what was actually fetched this wake (never
   optimistically past a source you skipped or that 429'd):
   ```python
   ws.set_cursor("teams_chat", "default", <this wake's run timestamp>)  # unchanged from
       # before this fix - checked, nothing in the codebase actually reads this one back
       # today (scheduled_refresh.py's own liveness check only watches calendar/sharepoint's
       # cursors, not this one). Kept for consistency with the other two sources rather than
       # removed outright. The per-chat cursors set inline in step 2 are the real mechanism
       # step 2's fairness re-sort reads back next wake.
   ws.set_cursor("calendar", "default", <the catch-up call's "now" boundary from step 3 -
       # NOT 14 days ahead. The lookahead window must stay perpetually un-advanced-past so
       # it's re-fetched fresh every wake, exactly as it already is.>)
   ws.set_cursor("sharepoint", "default", <this wake's run timestamp>)  # only if enabled+ran
   ```

6. **Run the normalizer.**
   ```
   python ingest/normalize.py
   ```
   This is a plain deterministic script — no reasoning needed, just run it.

7. **Emit a bus event** so `curator` knows there's fresh data to triage:
   ```python
   import bus
   bus.emit_event(source="relay", kind="ingest.raw_batch_ready", actor="relay")
   ```

8. **Report your status** (per the cockpit's worker_status mechanism):
   ```python
   ws.set_worker_status("relay", state="idle", current_task=None, detail="ingest wake complete")
   ```

9. **Stop.** Nothing else this wake. Do not triage, classify, or read what you just ingested
   for meaning — that's out of scope for this archetype (see `relay_role.md`).

## Safety net

`normalize.py` also runs on its own periodic sweep (every ~15 min, independent of relay's
wake cadence) in case relay's session dies mid-routine after writing drop files but before
step 6 — so a partial wake never permanently strands raw JSON in the inbox folder.
