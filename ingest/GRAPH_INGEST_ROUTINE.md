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

1. **Read cursors.** For each source in `teams_chat`, `calendar`, `sharepoint`:
   ```python
   import workgraph_store as ws
   cursor = ws.get_cursor(source, "default")  # None on first-ever wake
   ```

2. **Teams chats — capped and rate-limit-aware.**
   - Call `teams_list_chats` (paginate with `cursor` param if `moreResults` is true, but don't
     chase every page every wake — see the cap below).
   - **Do NOT filter by comparing `lastUpdatedDateTime` to the stored cursor** — that field is
     when the chat was renamed or its membership changed, NOT when it last had a message (per
     the tool's own description). A stable chat (same name, same members) can have brand-new
     messages every day while this field sits frozen from months ago — filtering on it silently
     and *permanently* excludes exactly the chats you talk in most, forever, with no error
     surfaced anywhere. Confirmed live 2026-07-29: a 3-person chat (Bhaumik Oza, RJ McLaughlin,
     Marc) with a `lastUpdatedDateTime` stuck on 2026-07-15 had a message land after 5pm the
     prior day that this filter dropped silently.
   - Instead: `teams_list_chats` results are already **ordered by most recent message first**
     (per the tool's own description) — just take the top N from that order. This is also *why*
     the cap below exists: trust the ordering, don't re-derive recency from a field that doesn't
     track it.
   - **Cap at 5-8 chats per wake, taken in the list's own returned order.** A 429 was hit on the
     very first attempt this session (retry-after 62s) pulling one chat's full messages — do not
     treat that as a fluke.
   - For each selected chat, call `read_resource` with URI
     `teams:///chats/{chatId}/messages` (confirmed working URI shape — no messageId needed
     to read a whole chat's messages).
   - **On a 429: stop pulling Teams for this wake entirely.** Do not retry-loop within one
     wake — write what you already have, update the cursor only past what was actually
     fetched, and let the next scheduled wake continue from there.
   - Write each chat's raw response verbatim (unmodified) to
     `new_cohort/data/raw_ingest_inbox/teams_chat_<unix_ts>.json`, one file per chat, as:
     ```json
     {"source": "teams_chat", "chat_id": "...", "chat_meta": {...from teams_list_chats...}, "messages_raw": {...from read_resource...}}
     ```

3. **Calendar — low volume, no pacing concern.**
   - Call `outlook_calendar_search` for the window `afterDateTime: "3 days ago"` through
     `beforeDateTime: "in 14 days"`.
   - Wrap the returned events into the envelope `normalize.py` expects (each event keeps its
     own fields as returned — `id`, `subject`, `organizer`, `attendees`, `start.dateTime`,
     `summary`, `seriesMasterId` if present — nothing renamed or reshaped):
     ```json
     {"source": "calendar", "events": [ {...one event...}, {...} ]}
     ```
   - Write to `new_cohort/data/raw_ingest_inbox/calendar_<unix_ts>.json`.

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
   - Write to `new_cohort/data/raw_ingest_inbox/sharepoint_<unix_ts>.json`.

5. **Update cursors** — only forward, only past what was actually fetched this wake (never
   optimistically past a source you skipped or that 429'd):
   ```python
   ws.set_cursor("teams_chat", "default", <newest lastUpdatedDateTime actually pulled>)
   ws.set_cursor("calendar", "default", <this wake's run timestamp>)
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
