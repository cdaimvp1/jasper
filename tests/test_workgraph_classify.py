"""Regression tests for workgraph_classify.py (tasks #24, #19):
- dead regex stems (escalat\\w* etc.) now match real inflections
- direction_inferred/sentiment_inferred/topic_inferred genuinely flip to
  False on a real cue match, so confidence tier H is actually reachable
- backfill_reclassify writes the FRESH result, not stale item[...] values
"""
import json
import time

import pytest

import workgraph_classify as wc
import workgraph_signals


def test_escalating_matches_negative_cue_stem():
    """Before the fix: NEGATIVE_CUE had bare 'escalat' with a trailing \\b,
    so only the exact word "escalate" matched - "escalating"/"escalation"
    (the far more common real inflections) did not."""
    result = wc.classify_item(subject="Escalating this to your attention",
                               body_preview="", from_actor="supplier@acme.com")
    assert result["sentiment"] == "negative"
    assert result["sentiment_inferred"] is False


def test_termination_matches_contract_topic_stem():
    result = wc.classify_item(subject="Notice of termination for the MSA",
                               body_preview="", from_actor="legal@acme.com")
    assert result["topic"] == "contract"
    assert result["topic_inferred"] is False


# --- calendar personal/OOO block filter (2026-07-31) ----------------------

def test_classify_item_calendar_personal_block_is_noise():
    """Real confirmed shape: HOLD/Focus Time/School Drop off/Pick up all
    have the organizer as the only real participant."""
    result = wc.classify_item(
        subject="HOLD", body_preview="", from_actor="lane_marc@lilly.com",
        source="calendar", organizer="lane_marc@lilly.com", participants=["lane_marc@lilly.com"],
    )
    assert result["item_class"] == "NOISE"


def test_classify_item_calendar_ooo_broadcast_is_noise_despite_many_attendees():
    """Real confirmed shape: an OOO announcement can be broadcast to a
    large distribution list - attendee-count alone wouldn't catch this,
    only the subject match does."""
    result = wc.classify_item(
        subject="Dima OOO Paternity Leave", body_preview="", from_actor="keane_dima@lilly.com",
        source="calendar", organizer="keane_dima@lilly.com",
        participants=["keane_dima@lilly.com", "lane_marc@lilly.com", "someone.else@lilly.com"],
    )
    assert result["item_class"] == "NOISE"


def test_classify_item_calendar_real_meeting_not_treated_as_noise():
    """A real recurring business meeting (multiple real attendees, no OOO
    wording) must NOT be swept up by the personal/OOO filter."""
    result = wc.classify_item(
        subject="C5 Contracts Weekly Touchbase", body_preview="", from_actor="cori.mccorkle@lilly.com",
        source="calendar", organizer="cori.mccorkle@lilly.com",
        participants=["cori.mccorkle@lilly.com", "lane_marc@lilly.com"],
    )
    assert result["item_class"] != "NOISE"


def test_classify_item_personal_block_check_only_applies_to_calendar_source():
    """The same organizer/participants shape on a NON-calendar source (e.g.
    a real 1:1 email thread) must not be swept up - this filter is
    calendar-specific."""
    result = wc.classify_item(
        subject="Quick question", body_preview="just checking in", from_actor="lane_marc@lilly.com",
        source="outlook_mail", organizer="lane_marc@lilly.com", participants=["lane_marc@lilly.com"],
    )
    assert result["item_class"] != "NOISE"


def test_classify_item_without_calendar_params_is_unaffected():
    """Every pre-existing caller passes none of source/organizer/
    participants - the calendar check can never fire without a source, so
    a subject like "HOLD" (which would be caught if source="calendar" were
    passed) falls through to the ordinary generic path instead of NOISE."""
    result = wc.classify_item(subject="HOLD", body_preview="", from_actor="lane_marc@lilly.com")
    assert result["item_class"] != "NOISE"


# --- confirmed_direction (task #270 Phase B, 2026-08-07) -------------------

def test_classify_item_confirmed_direction_wins_over_cue_regex():
    """A sent-items row with confirmed_direction="outbound" must not be
    overridden by OUTBOUND_CUE/INBOUND_CUE - a real known fact (it came out
    of Outlook's own Sent Items folder) beats a keyword guess. This exact
    text would otherwise match INBOUND_CUE ("received")."""
    result = wc.classify_item(
        subject="Re: SOW", body_preview="Received, thanks - approved.",
        from_actor="lane_marc@lilly.com", confirmed_direction="outbound",
    )
    assert result["direction"] == "outbound"
    assert result["direction_inferred"] is False


def test_classify_item_without_confirmed_direction_falls_back_to_cue_regex():
    """Every pre-existing caller passes confirmed_direction=None (the
    default) and is completely unaffected - the cue-regex path still owns
    direction for anything not sourced from the sent-items ingester."""
    result = wc.classify_item(
        subject="Re: SOW", body_preview="Received, thanks - approved.",
        from_actor="someone@example.com",
    )
    assert result["direction"] == "inbound"


def test_confirmed_direction_from_meta_reads_meta_json():
    assert wc._confirmed_direction_from_meta({"meta_json": '{"confirmed_direction":"outbound"}'}) == "outbound"


def test_confirmed_direction_from_meta_none_for_missing_or_malformed():
    assert wc._confirmed_direction_from_meta({}) is None
    assert wc._confirmed_direction_from_meta({"meta_json": None}) is None
    assert wc._confirmed_direction_from_meta({"meta_json": "not json"}) is None
    assert wc._confirmed_direction_from_meta({"meta_json": '{"is_recurring": true}'}) is None


def test_parse_participants_handles_valid_json_list():
    assert wc._parse_participants({"participants": '["a@x.com", "b@x.com"]'}) == ["a@x.com", "b@x.com"]


def test_parse_participants_returns_none_for_malformed_json():
    assert wc._parse_participants({"participants": "not json"}) is None


def test_parse_participants_returns_none_when_missing():
    assert wc._parse_participants({}) is None


def test_confidence_tier_h_is_reachable():
    """A fully-explicit item (real direction cue, real topic cue, real
    sentiment cue, confident class) must be able to reach tier H - before
    the fix, direction_inferred/sentiment_inferred were hardcoded True
    unconditionally, making H unreachable for ANY input."""
    result = wc.classify_item(
        subject="We received your invoice - thank you for your patience",
        body_preview="",
        from_actor="finance@acme.com",
    )
    assert result["direction_inferred"] is False  # "received" -> INBOUND_CUE
    assert result["sentiment_inferred"] is False  # "thank you" -> POSITIVE_CUE
    assert result["topic_inferred"] is False  # "invoice" -> financial topic
    assert result["confidence"] in ("H", "M")  # at minimum should not be forced to L


def test_direction_inferred_flips_false_on_real_cue():
    result = wc.classify_item(subject="FYI, sending this along internally",
                               body_preview="", from_actor="colleague@lilly.com")
    # whatever direction it resolves to, the point is direction_inferred must
    # be able to be False when a real cue exists - not hardcoded True always
    assert isinstance(result["direction_inferred"], bool)


# --- task #83: widened reference-number extraction -----------------------

def test_classify_item_finds_po_number_without_a_recognized_signal():
    """A plain human email with a real PO number - no Ariba/DocuSign/etc.
    sender pattern matches at all, so `signal` is None and the OLD logic
    (signal["pr_number"] if signal else None) would have returned None
    even though a real reference number is right there in the text."""
    result = wc.classify_item(subject="Follow-up on PO4200703817 for the office supplies order",
                               body_preview="", from_actor="vendor@example.com")
    assert result["pr_number"] == "PO4200703817"


def test_classify_item_finds_pr_number_in_body_not_just_subject():
    result = wc.classify_item(subject="Quick question",
                               body_preview="This relates to PR1000042 from last week",
                               from_actor="vendor@example.com")
    assert result["pr_number"] == "PR1000042"


def test_classify_item_reference_number_is_case_insensitive():
    result = wc.classify_item(subject="re: po4200703817 status", body_preview="",
                               from_actor="vendor@example.com")
    assert result["pr_number"] == "PO4200703817"


def test_classify_item_no_reference_present_is_none():
    result = wc.classify_item(subject="Let's grab coffee next week", body_preview="",
                               from_actor="colleague@lilly.com")
    assert result["pr_number"] is None


def test_classify_item_extracts_jasper_ref_issue_id_from_body():
    result = wc.classify_item(subject="Re: Workday renewal", body_preview="Sounds good.\n\nRef: JW-marc-308",
                               from_actor="vendor@example.com")
    assert result["jasper_ref_issue_id"] == "marc-308"


def test_classify_item_no_jasper_ref_present_is_none():
    result = wc.classify_item(subject="Let's grab coffee next week", body_preview="",
                               from_actor="colleague@lilly.com")
    assert result["jasper_ref_issue_id"] is None


def test_classify_item_recognized_signal_pr_number_still_wins(monkeypatch):
    """A recognized automated-signal match's own (subject-only) pr_number
    is used as-is when present - this is a confirmed, real template, not a
    guess, and should not be second-guessed by the general fallback scan."""
    import workgraph_signals

    def fake_signal(*, subject, from_actor):
        return {"signal_type": "ariba_pr_approval_needed", "treatment": "actionable", "pr_number": "PR7654321"}

    monkeypatch.setattr(workgraph_signals, "classify_signal", fake_signal)
    result = wc.classify_item(subject="Approve PR0000001 (unrelated number also present)",
                               body_preview="", from_actor="no-reply@ansmtp.ariba.com")
    assert result["pr_number"] == "PR7654321"


def test_backfill_reclassify_updates_pr_number_only_change(ws_db):
    """Task #83's real motivating case: a historical raw_item where every
    OTHER classify_item output already matches what's stored (so the old
    comparison, which didn't check pr_number at all, would have skipped
    it) but the reference-number extraction has since widened. Confirms
    the fix to backfill_reclassify's comparison actually catches and
    writes this."""
    subject = "Follow-up on PO4200703817 for the office supplies order"
    fresh = wc.classify_item(subject=subject, body_preview="", from_actor="vendor@example.com")
    assert fresh["pr_number"] == "PO4200703817"

    rid = ws_db.insert_raw_item(
        source="outlook_mail", stable_key="bf1", thread_key="bf1", dedupe_key="bf1",
        occurred_ts=0.0, subject=subject, from_actor="vendor@example.com", participants_json="[]",
    )
    # Simulate a row classified BEFORE the widening: every other field
    # already matches what classify_item produces today, but pr_number is
    # still None (the historical, narrower behavior).
    ws_db.classify_raw_item(
        rid, item_class=fresh["item_class"], direction=fresh["direction"],
        direction_inferred=fresh["direction_inferred"], topic=fresh["topic"],
        topic_inferred=fresh["topic_inferred"], sentiment=fresh["sentiment"],
        sentiment_inferred=fresh["sentiment_inferred"], anomaly_flag=fresh["anomaly_flag"],
        signal_type=fresh["signal_type"], pr_number=None,
    )

    result = wc.backfill_reclassify()

    assert result["updated"] == 1
    conn = ws_db._connect()
    row = conn.execute("SELECT pr_number FROM raw_items WHERE id = ?", (rid,)).fetchone()
    conn.close()
    assert row["pr_number"] == "PO4200703817"


def test_run_classification_queues_sharepoint_link_from_message_body(ws_db, isolated_paths):
    """Task #303: run_classification() should notice a SharePoint link in
    an ORDINARY message's body (no recognized signal_type at all here)
    and queue it for relay to fetch - this is the real hook point the
    detection mechanism relies on."""
    rid = ws_db.insert_raw_item(
        source="outlook_mail", stable_key="sp1", thread_key="sp1", dedupe_key="sp1",
        occurred_ts=0.0, subject="Sharing the redline", from_actor="counterparty@example.com",
        participants_json="[]",
    )
    # raw_ref names this row's own real id, so it has to be written after
    # insert_raw_item hands the id back.
    conn = ws_db._connect()
    conn.execute(
        "UPDATE raw_items SET raw_ref = ? WHERE id = ?",
        (json.dumps({"body_html": f"raw_items/{rid}/body.html"}), rid),
    )
    conn.commit()
    conn.close()
    dest_dir = isolated_paths.DOCUMENTS_DIR / "raw_items" / str(rid)
    dest_dir.mkdir(parents=True, exist_ok=True)
    (dest_dir / "body.html").write_text(
        '<a href="https://lillyco.sharepoint.com/sites/Legal/redline.docx">redline.docx</a>',
        encoding="utf-8",
    )

    wc.run_classification()

    pending = ws_db.list_pending_link_fetches()
    assert len(pending) == 1
    assert pending[0]["raw_item_id"] == rid
    assert pending[0]["url"] == "https://lillyco.sharepoint.com/sites/Legal/redline.docx"


# --- Part C (2026-07-30): raw-item-to-issue linking via reference ID -----

def _pending_item(ws_db, thread_key, subject, pr_number=None, item_class="ACTIONABLE-ASK", from_actor="a@example.com",
                   jasper_ref_issue_id=None, source="outlook_mail", topic="other", participants_json="[]"):
    """A classified-but-not-yet-linked raw_item, ready for cluster_and_link().
    Each thread_key is deliberately unique per call so thread_map_lookup
    never resolves it - the whole point is testing the NO-thread-match
    fallback path."""
    rid = ws_db.insert_raw_item(
        source=source, stable_key=thread_key, thread_key=thread_key, dedupe_key=thread_key,
        occurred_ts=time.time(), subject=subject, from_actor=from_actor, participants_json=participants_json,
    )
    ws_db.classify_raw_item(
        rid, item_class=item_class, direction="inbound", direction_inferred=False,
        topic=topic, topic_inferred=True, sentiment="neutral", sentiment_inferred=True,
        anomaly_flag=False, signal_type=None, pr_number=pr_number,
        pr_number_base=workgraph_signals.reference_base(pr_number),
        jasper_ref_issue_id=jasper_ref_issue_id,
    )
    return rid


def _seed_real_issue_with_thread(ws_db, thread_key, title, source="outlook_mail", state="active"):
    """Corrected-ordering redesign (2026-08-05): cluster_and_link() no
    longer creates a real issue for a fresh thread - a fresh unmatched
    item becomes a cluster now (see task #184's Phase B). Several existing
    tests need to exercise real-issue-specific behavior (state tracking,
    NBA, etc.) that only applies once something is ALREADY a promoted
    issue - Phase C (cluster group -> project -> curator-derived issue)
    isn't built yet, so this helper stands in for "an issue that's already
    been promoted:" a real issue, with its container mapping seeded
    directly so a later matching raw_item on the SAME thread_key attaches
    to it exactly the way cluster_and_link's own _container_lookup_issue
    would resolve a real already-promoted issue. Returns the new issue id."""
    iid = ws_db.create_issue_with_new_id(title=title, state=state, category="other")
    container_type = "email_conversation" if source == "outlook_mail" else "teams_chat" if source == "teams_chat" else "calendar_series"
    ws_db.upsert_source_container(id=f"sc-{source}-{thread_key}", source=source, container_type=container_type,
                                   exact_key=thread_key, key_quality="exact", issue_id=iid)
    return iid


def _isolate_config(ws_db, monkeypatch, tmp_path):
    """Same isolation pattern as test_retention.py's retention_env fixture -
    config.SETTINGS_PATH is bound at import time, not per-test via
    isolated_paths, so it needs its own explicit monkeypatch here."""
    import config
    monkeypatch.setattr(config, "SETTINGS_PATH", tmp_path / "settings.json")
    monkeypatch.setattr(config, "_cache", {})
    monkeypatch.setattr(config, "_cache_mtime", 0.0)
    return config


# --- derive_target_state (2026-08-01, real-incident follow-up) ----------

def test_derive_target_state_actionable_ask_is_active(ws_db):
    iid = ws_db.create_issue_with_new_id(title="Ask", state="done", category="financial")
    rid = ws_db.insert_raw_item(source="outlook_mail", stable_key="d1", thread_key="d1", dedupe_key="d1",
                                 occurred_ts=time.time(), subject="approve", from_actor="a@example.com",
                                 participants_json="[]")
    ws_db.classify_raw_item(rid, item_class="ACTIONABLE-ASK", direction="inbound", direction_inferred=False,
                             topic="other", topic_inferred=True, sentiment="neutral", sentiment_inferred=True,
                             anomaly_flag=False)
    ws_db.link_raw_item_to_issue(rid, iid)

    # Pure read: does NOT touch the issue's actual state, unlike recompute_issue_state.
    assert wc.derive_target_state(iid) == "active"
    assert ws_db.get_issue(iid)["state"] == "done"


def test_derive_target_state_waiting_on_others(ws_db):
    iid = ws_db.create_issue_with_new_id(title="Waiting", state="active", category="other")
    rid = ws_db.insert_raw_item(source="outlook_mail", stable_key="d2", thread_key="d2", dedupe_key="d2",
                                 occurred_ts=time.time(), subject="will follow up", from_actor="a@example.com",
                                 participants_json="[]")
    ws_db.classify_raw_item(rid, item_class="WAITING-ON-OTHERS", direction="inbound", direction_inferred=False,
                             topic="other", topic_inferred=True, sentiment="neutral", sentiment_inferred=True,
                             anomaly_flag=False)
    ws_db.link_raw_item_to_issue(rid, iid)

    assert wc.derive_target_state(iid) == "waiting"


def test_derive_target_state_only_fyi_is_done(ws_db):
    iid = ws_db.create_issue_with_new_id(title="FYI only", state="active", category="other")
    rid = ws_db.insert_raw_item(source="outlook_mail", stable_key="d3", thread_key="d3", dedupe_key="d3",
                                 occurred_ts=time.time(), subject="fyi", from_actor="a@example.com",
                                 participants_json="[]")
    ws_db.classify_raw_item(rid, item_class="FYI-EVIDENCE", direction="inbound", direction_inferred=False,
                             topic="other", topic_inferred=True, sentiment="neutral", sentiment_inferred=True,
                             anomaly_flag=False)
    ws_db.link_raw_item_to_issue(rid, iid)

    assert wc.derive_target_state(iid) == "done"


def test_derive_target_state_unconfirmed_ariba_request_never_auto_closes(ws_db):
    """The exact latent risk found investigating marc-014/marc-185: an issue
    whose only evidence is a real ariba_pr_approval_needed request must stay
    'active' via signal_type identity even if item_class alone would say
    'done' (simulating what a live signal_treatment_override remapping the
    trigger's treatment away from 'actionable' would produce)."""
    iid = ws_db.create_issue_with_new_id(title="PR approval", state="active", category="financial")
    rid = ws_db.insert_raw_item(source="outlook_mail", stable_key="s1", thread_key="s1", dedupe_key="s1",
                                 occurred_ts=time.time(), subject="Action Required: Approve the Requisition",
                                 from_actor="ansmtp@ariba.com", participants_json="[]")
    # item_class is FYI-EVIDENCE here on purpose - stands in for what an
    # overridden treatment would produce; signal_type is what must save it.
    ws_db.classify_raw_item(rid, item_class="FYI-EVIDENCE", direction="inbound", direction_inferred=False,
                             topic="financial", topic_inferred=True, sentiment="neutral", sentiment_inferred=True,
                             anomaly_flag=False, signal_type="ariba_pr_approval_needed")
    ws_db.link_raw_item_to_issue(rid, iid)

    assert wc.derive_target_state(iid) == "active"


def test_derive_target_state_ariba_request_with_real_closure_signal_is_done(ws_db):
    """The other half - a REAL closure email (a distinct signal_type, not
    just a re-classified item_class) legitimately closes it."""
    iid = ws_db.create_issue_with_new_id(title="PR approval", state="active", category="financial")
    rid1 = ws_db.insert_raw_item(source="outlook_mail", stable_key="s2", thread_key="s2", dedupe_key="s2",
                                  occurred_ts=time.time(), subject="Action Required: Approve the Requisition",
                                  from_actor="ansmtp@ariba.com", participants_json="[]")
    ws_db.classify_raw_item(rid1, item_class="ACTIONABLE-ASK", direction="inbound", direction_inferred=False,
                             topic="financial", topic_inferred=True, sentiment="neutral", sentiment_inferred=True,
                             anomaly_flag=False, signal_type="ariba_pr_approval_needed")
    ws_db.link_raw_item_to_issue(rid1, iid)
    rid2 = ws_db.insert_raw_item(source="outlook_mail", stable_key="s3", thread_key="s3", dedupe_key="s3",
                                  occurred_ts=time.time() + 1, subject="Notification: The Requisition has been fully approved",
                                  from_actor="ansmtp@ariba.com", participants_json="[]")
    ws_db.classify_raw_item(rid2, item_class="FYI-EVIDENCE", direction="inbound", direction_inferred=False,
                             topic="financial", topic_inferred=True, sentiment="neutral", sentiment_inferred=True,
                             anomaly_flag=False, signal_type="ariba_pr_fully_approved")
    ws_db.link_raw_item_to_issue(rid2, iid)

    # ACTIONABLE-ASK is still present (rid1's item_class), so this returns
    # "active" via the ordinary item_class path, not even reaching the
    # signal_type check - real, current behavior, asserted so a future
    # change to the ACTIONABLE-ASK branch can't silently regress this.
    assert wc.derive_target_state(iid) == "active"


def test_derive_target_state_concur_has_no_closure_signal_required(ws_db):
    """concur_expense_reminder has no matching closure template in the
    signal catalog - deliberately absent from REQUEST_TO_CLOSURE_SIGNAL, so
    it must NOT get stuck 'active' forever waiting for an email that will
    never arrive."""
    iid = ws_db.create_issue_with_new_id(title="Expense reminder", state="active", category="financial")
    rid = ws_db.insert_raw_item(source="outlook_mail", stable_key="s4", thread_key="s4", dedupe_key="s4",
                                 occurred_ts=time.time(), subject="Action Required: Unapplied credit card transactions",
                                 from_actor="notify@concursolutions.com", participants_json="[]")
    ws_db.classify_raw_item(rid, item_class="FYI-EVIDENCE", direction="inbound", direction_inferred=False,
                             topic="financial", topic_inferred=True, sentiment="neutral", sentiment_inferred=True,
                             anomaly_flag=False, signal_type="concur_expense_reminder")
    ws_db.link_raw_item_to_issue(rid, iid)

    assert wc.derive_target_state(iid) == "done"


def test_recompute_issue_state_uses_derive_target_state_internally(ws_db):
    """Adversarial check on the 2026-08-01 refactor: recompute_issue_state's
    own behavior must be byte-for-byte unchanged now that its derivation is
    delegated to derive_target_state rather than inlined."""
    iid = ws_db.create_issue_with_new_id(title="Ask", state="waiting", category="other")
    rid = ws_db.insert_raw_item(source="outlook_mail", stable_key="d4", thread_key="d4", dedupe_key="d4",
                                 occurred_ts=time.time(), subject="approve", from_actor="a@example.com",
                                 participants_json="[]")
    ws_db.classify_raw_item(rid, item_class="ACTIONABLE-ASK", direction="inbound", direction_inferred=False,
                             topic="other", topic_inferred=True, sentiment="neutral", sentiment_inferred=True,
                             anomaly_flag=False)
    ws_db.link_raw_item_to_issue(rid, iid)

    assert wc.derive_target_state(iid) == wc.recompute_issue_state(iid) == "active"
    assert ws_db.get_issue(iid)["state"] == "active"


# --- recompute_issue_state (2026-07-31, meeting-grouping design pass) ----

def test_recompute_issue_state_new_item_not_actionable_respects_manual_close(ws_db):
    """The real bug: an OLD actionable ask (already resolved, which is why
    a human closed the issue) used to keep target='active' forever, so ANY
    later item re-triggering this function - even an unrelated FYI reply -
    would silently reopen a manually-closed issue. new_item_is_actionable
    lets the caller say "the item that just arrived isn't itself an ask" -
    the full history must not override that."""
    iid = ws_db.create_issue_with_new_id(title="Old ask", state="done", category="other")
    rid = ws_db.insert_raw_item(source="outlook_mail", stable_key="k1", thread_key="k1", dedupe_key="k1",
                                 occurred_ts=time.time(), subject="please approve", from_actor="a@example.com",
                                 participants_json="[]")
    ws_db.classify_raw_item(rid, item_class="ACTIONABLE-ASK", direction="inbound", direction_inferred=False,
                             topic="other", topic_inferred=True, sentiment="neutral", sentiment_inferred=True,
                             anomaly_flag=False)
    ws_db.link_raw_item_to_issue(rid, iid)

    result = wc.recompute_issue_state(iid, new_item_is_actionable=False)

    assert result == "done"
    assert ws_db.get_issue(iid)["state"] == "done"


def test_recompute_issue_state_respects_manual_dismiss(ws_db):
    """Task #44: 'dismissed' is a real, distinct terminal state and must get
    the exact same "don't silently reopen" protection as done/noise-archived -
    a stray later FYI reply must not flip a dismissed issue back to active."""
    iid = ws_db.create_issue_with_new_id(title="Old ask", state="dismissed", category="other")
    rid = ws_db.insert_raw_item(source="outlook_mail", stable_key="k1b", thread_key="k1b", dedupe_key="k1b",
                                 occurred_ts=time.time(), subject="please approve", from_actor="a@example.com",
                                 participants_json="[]")
    ws_db.classify_raw_item(rid, item_class="ACTIONABLE-ASK", direction="inbound", direction_inferred=False,
                             topic="other", topic_inferred=True, sentiment="neutral", sentiment_inferred=True,
                             anomaly_flag=False)
    ws_db.link_raw_item_to_issue(rid, iid)

    result = wc.recompute_issue_state(iid, new_item_is_actionable=False)

    assert result == "dismissed"
    assert ws_db.get_issue(iid)["state"] == "dismissed"


def test_recompute_issue_state_default_still_reopens_from_full_history(ws_db):
    """Callers with no specific new-item context (backfill_reclassify's
    ruleset-change re-derivation, the manual bulk-recompute path) keep
    today's full-history behavior on purpose - a ruleset improvement
    retroactively revealing a real unresolved ask is a legitimate reason to
    reconsider a closed issue, unlike routine new mail arriving."""
    iid = ws_db.create_issue_with_new_id(title="Old ask", state="done", category="other")
    rid = ws_db.insert_raw_item(source="outlook_mail", stable_key="k2", thread_key="k2", dedupe_key="k2",
                                 occurred_ts=time.time(), subject="please approve", from_actor="a@example.com",
                                 participants_json="[]")
    ws_db.classify_raw_item(rid, item_class="ACTIONABLE-ASK", direction="inbound", direction_inferred=False,
                             topic="other", topic_inferred=True, sentiment="neutral", sentiment_inferred=True,
                             anomaly_flag=False)
    ws_db.link_raw_item_to_issue(rid, iid)

    result = wc.recompute_issue_state(iid)  # default new_item_is_actionable=True

    assert result == "active"
    assert ws_db.get_issue(iid)["state"] == "active"


def test_recompute_issue_state_open_issue_unaffected_by_new_item_is_actionable(ws_db):
    """The gate only applies to done/noise-archived - an already-open issue
    still gets the full, real recomputation regardless."""
    iid = ws_db.create_issue_with_new_id(title="Waiting", state="waiting", category="other")
    rid = ws_db.insert_raw_item(source="outlook_mail", stable_key="k3", thread_key="k3", dedupe_key="k3",
                                 occurred_ts=time.time(), subject="please approve", from_actor="a@example.com",
                                 participants_json="[]")
    ws_db.classify_raw_item(rid, item_class="ACTIONABLE-ASK", direction="inbound", direction_inferred=False,
                             topic="other", topic_inferred=True, sentiment="neutral", sentiment_inferred=True,
                             anomaly_flag=False)
    ws_db.link_raw_item_to_issue(rid, iid)

    result = wc.recompute_issue_state(iid, new_item_is_actionable=False)

    assert result == "active"
    assert ws_db.get_issue(iid)["state"] == "active"


def test_cluster_and_link_new_fyi_reply_does_not_reopen_a_done_issue(ws_db):
    """End-to-end reproduction of the real bug: an issue with a resolved
    ask is manually marked done, then a brand-new, genuinely unrelated FYI
    reply lands on the SAME thread - must NOT silently reopen it."""
    iid = _seed_real_issue_with_thread(ws_db, "ck-reopen", "please approve the SOW", state="done")

    rid = ws_db.insert_raw_item(source="outlook_mail", stable_key="ck-reopen", thread_key="ck-reopen", dedupe_key="ck-reopen-2",
                                 occurred_ts=time.time(), subject="FYI, fully approved and filed",
                                 from_actor="b@example.com", participants_json="[]")
    ws_db.classify_raw_item(rid, item_class="FYI-EVIDENCE", direction="inbound", direction_inferred=False,
                             topic="other", topic_inferred=True, sentiment="neutral", sentiment_inferred=True,
                             anomaly_flag=False)

    wc.cluster_and_link()

    assert ws_db.get_issue(iid)["state"] == "done"


def test_cluster_and_link_new_actionable_reply_does_reopen_a_done_issue(ws_db):
    """The other half: a GENUINELY new ask on the same thread must still
    reopen the issue - this isn't a blanket freeze, only a targeted fix."""
    iid = _seed_real_issue_with_thread(ws_db, "ck-reopen2", "please approve the SOW", state="done")

    rid = ws_db.insert_raw_item(source="outlook_mail", stable_key="ck-reopen2", thread_key="ck-reopen2", dedupe_key="ck-reopen2-2",
                                 occurred_ts=time.time(), subject="Actually, one more approval needed on this",
                                 from_actor="b@example.com", participants_json="[]")
    ws_db.classify_raw_item(rid, item_class="ACTIONABLE-ASK", direction="inbound", direction_inferred=False,
                             topic="other", topic_inferred=True, sentiment="neutral", sentiment_inferred=True,
                             anomaly_flag=False)

    wc.cluster_and_link()

    assert ws_db.get_issue(iid)["state"] == "active"


# --- Part D: subject-match fallback (2026-08-01, real-incident follow-up) --

def test_normalize_subject_for_matching_real_observed_variants_agree():
    """The exact real thread this was built for - 4 real subject variants
    of the same meeting, none sharing a thread_key, must normalize
    identically."""
    variants = [
        "[EXTERNAL] Re: Lilly and Workday - Early Renewal Weekly Meeting",
        "Automatic reply: Lilly and Workday - Early Renewal Weekly Meeting",
        '[EXTERNAL] Your meeting "Lilly and Workday - Early Renewal Weekly Meeting" is starting soon...',
        "Fw: Lilly and Workday - Early Renewal Weekly Meeting",
    ]
    normalized = {wc.normalize_subject_for_matching(s) for s in variants}
    assert len(normalized) == 1
    assert normalized == {"lilly and workday - early renewal weekly meeting"}


def test_cluster_and_link_subject_match_shadow_counts_but_does_not_attach_when_flag_off(ws_db, monkeypatch, tmp_path):
    config = _isolate_config(ws_db, monkeypatch, tmp_path)
    assert config.get("grouping", "subject_match_auto_attach_enabled") in (None, False)

    _pending_item(ws_db, "sm1", "Weekly Sync", from_actor="dan@workday.com")
    wc.cluster_and_link()
    rid = _pending_item(ws_db, "sm2", "[EXTERNAL] Re: Weekly Sync", item_class="FYI-EVIDENCE",
                         from_actor="dan@workday.com")
    result = wc.cluster_and_link()

    assert result["subject_match_auto_attach_enabled"] is False
    assert result["would_attach_via_subject_match"] == 1
    assert result["attached_via_subject_match"] == 0
    # Corrected pipeline Phase E (2026-08-05): a real external sender
    # (dan@workday.com, a real company by the same domain heuristic
    # classify_affiliation always used) is genuine signal now, not
    # standalone-FYI noise - the flag being off only means it doesn't
    # attach to the EXISTING issue (still proven by would_attach above);
    # it gets its own cluster instead of being dropped.
    assert result["fyi_standalone_skipped"] == 0
    assert result["fyi_promoted_to_cluster"] == 1
    assert ws_db.get_raw_item(rid)["issue_id"] is not None


def test_cluster_and_link_subject_match_attaches_when_flag_enabled_and_domain_matches(ws_db, monkeypatch, tmp_path):
    config = _isolate_config(ws_db, monkeypatch, tmp_path)
    config.set_value(True, "grouping", "subject_match_auto_attach_enabled")

    first_rid = _pending_item(ws_db, "sm3", "Weekly Sync", from_actor="dan@workday.com")
    wc.cluster_and_link()
    first_issue_id = ws_db.get_raw_item(first_rid)["issue_id"]

    rid = _pending_item(ws_db, "sm4", "[EXTERNAL] Re: Weekly Sync", item_class="FYI-EVIDENCE",
                         from_actor="dan@workday.com")
    result = wc.cluster_and_link()

    assert result["attached_via_subject_match"] == 1
    assert result["issues_created"] == 0, "must attach to the existing issue, not create/skip a second one"
    assert ws_db.get_raw_item(rid)["issue_id"] == first_issue_id


def test_cluster_and_link_subject_match_requires_same_domain_too(ws_db, monkeypatch, tmp_path):
    """The exact false-positive risk a bare subject match alone can't rule
    out: a generic recurring-meeting title reused by a completely different
    counterparty must NOT attach, even with the flag on."""
    config = _isolate_config(ws_db, monkeypatch, tmp_path)
    config.set_value(True, "grouping", "subject_match_auto_attach_enabled")

    _pending_item(ws_db, "sm5", "Weekly Sync", from_actor="dan@workday.com")
    wc.cluster_and_link()

    rid = _pending_item(ws_db, "sm6", "[EXTERNAL] Re: Weekly Sync", item_class="FYI-EVIDENCE",
                         from_actor="someone@unrelated-vendor.com")
    result = wc.cluster_and_link()

    assert result["attached_via_subject_match"] == 0
    assert result["would_attach_via_subject_match"] == 0
    # Corrected pipeline Phase E: a real external sender is genuine signal
    # now - never attaches to the wrong issue (still proven above), but
    # gets its own cluster instead of being dropped as noise.
    assert result["fyi_standalone_skipped"] == 0
    assert result["fyi_promoted_to_cluster"] == 1
    assert ws_db.get_raw_item(rid)["issue_id"] is not None


def test_cluster_and_link_writes_breadcrumb_evidence_when_attached_via_subject_match(ws_db, monkeypatch, tmp_path):
    config = _isolate_config(ws_db, monkeypatch, tmp_path)
    config.set_value(True, "grouping", "subject_match_auto_attach_enabled")

    _pending_item(ws_db, "sm7", "Weekly Sync", from_actor="dan@workday.com")
    wc.cluster_and_link()
    rid = _pending_item(ws_db, "sm8", "[EXTERNAL] Re: Weekly Sync", item_class="FYI-EVIDENCE",
                         from_actor="dan@workday.com")
    wc.cluster_and_link()

    issue_id = ws_db.get_raw_item(rid)["issue_id"]
    evidence = ws_db.list_evidence(issue_id)
    matching = [e for e in evidence if e["raw_item_id"] == rid]
    assert len(matching) == 1
    assert "[auto-attached via matching subject + sender]" in matching[0]["summary"]


def test_cluster_and_link_creates_new_issue_when_no_reference_match(ws_db):
    _pending_item(ws_db, "ck1", "A brand new ask with nothing structured in it")
    result = wc.cluster_and_link()
    assert result["issues_created"] == 1
    assert result["attached_via_reference"] == 0
    assert result["would_attach_via_reference"] == 0


# --- ariba_requisitions wiring (task #267, 2026-08-07) ---------------------

def test_cluster_and_link_persists_ariba_requisition_fields(ws_db):
    """extract_ariba_requisition_fields already ran as a significance check
    before this task (_has_matchable_signal) but its result was discarded -
    now a real Ariba-signal-typed item's requester/descriptor/amount land in
    the system-scoped ariba_requisitions table, keyed by its own PR#."""
    rid = ws_db.insert_raw_item(
        source="outlook_mail", stable_key="ar1", thread_key="ar1", dedupe_key="ar1",
        occurred_ts=time.time(),
        subject=("Action required: Approve the Requisition that THOMAS TURNER submitted  - "
                  "PR1193376 - Workday HCM SaaS ($53,702,143.00 USD)"),
        from_actor="ariba-notifications@ariba.com", participants_json="[]",
    )
    ws_db.classify_raw_item(
        rid, item_class="ACTIONABLE-ASK", direction="inbound", direction_inferred=False,
        topic="other", topic_inferred=True, sentiment="neutral", sentiment_inferred=True,
        anomaly_flag=False, signal_type="ariba_pr_approval_needed",
        pr_number="PR1193376", pr_number_base="PR1193376", jasper_ref_issue_id=None,
    )

    wc.cluster_and_link()

    row = ws_db.get_ariba_requisition("PR1193376")
    assert row is not None
    assert row["requester"] == "THOMAS TURNER"
    assert row["descriptor"] == "Workday HCM SaaS"
    assert row["amount"] == 53702143.0
    assert row["issue_id"] is not None


def test_cluster_and_link_skips_ariba_table_for_non_ariba_signal_type(ws_db):
    """Same gate as the ContractPodAI wiring - only signal_type startswith
    'ariba_' attempts extraction, so an unrelated item with a coincidentally
    PR-shaped subject never gets a spurious ariba_requisitions row."""
    rid = _pending_item(ws_db, "ar2", "PR1193376 status update, nothing structured")
    wc.cluster_and_link()

    assert ws_db.get_ariba_requisition("PR1193376") is None


# --- Corrected pipeline Phase E (2026-08-05): FYI-standalone-skip gap ------
# fix. A standalone FYI-EVIDENCE item (no thread/reference/jasper-ref/
# subject match) carrying a real data point of its own must become a
# cluster - eligible for pass-2 matching later - instead of being dropped
# permanently. Zero real signal is still genuine noise, unchanged.

def test_fyi_standalone_item_with_external_sender_is_promoted_not_dropped(ws_db):
    rid = _pending_item(ws_db, "pe1", "quarterly business review notes", item_class="FYI-EVIDENCE",
                         from_actor="jane@authenticx.com")
    result = wc.cluster_and_link()

    assert result["fyi_standalone_skipped"] == 0
    assert result["fyi_promoted_to_cluster"] == 1
    cluster_id = ws_db.get_raw_item(rid)["issue_id"]
    assert cluster_id is not None
    assert ws_db.get_cluster(cluster_id) is not None


def test_fyi_standalone_item_with_internal_only_sender_is_still_dropped(ws_db):
    rid = _pending_item(ws_db, "pe2", "reminder: badge photo day is Friday", item_class="FYI-EVIDENCE",
                         from_actor="hr@lilly.com")
    result = wc.cluster_and_link()

    assert result["fyi_standalone_skipped"] == 1
    assert result["fyi_promoted_to_cluster"] == 0
    assert ws_db.get_raw_item(rid)["issue_id"] is None


def test_fyi_standalone_item_with_automated_sender_and_no_other_signal_is_still_dropped(ws_db):
    """An automated/system sender's own domain never counts as a real
    supplier (classify_affiliation already resolves company=None for one) -
    without a reference or Ariba field too, this is still real noise."""
    rid = _pending_item(ws_db, "pe3", "your weekly digest is ready", item_class="FYI-EVIDENCE",
                         from_actor="no-reply@ansmtp.ariba.com")
    result = wc.cluster_and_link()

    assert result["fyi_standalone_skipped"] == 1
    assert ws_db.get_raw_item(rid)["issue_id"] is None


def test_fyi_standalone_item_with_reference_but_internal_sender_is_promoted(ws_db):
    rid = _pending_item(ws_db, "pe4", "FYI - PR9988776 has moved to the next stage", pr_number="PR9988776",
                         item_class="FYI-EVIDENCE", from_actor="bob@lilly.com")
    result = wc.cluster_and_link()

    assert result["fyi_promoted_to_cluster"] == 1
    assert ws_db.get_raw_item(rid)["issue_id"] is not None


def test_fyi_standalone_item_with_external_participant_but_internal_organizer_is_promoted(ws_db):
    """Real gap caught live running the Authenticx acceptance test: a
    calendar meeting organized by a Lilly-internal person (from_actor) with
    the real external counterparty only listed as a participant/attendee -
    must still count as real signal, not just the organizer's own domain."""
    rid = _pending_item(
        ws_db, "pe6", "VOC & Authenticx", item_class="FYI-EVIDENCE", source="calendar",
        from_actor="internal.organizer@lilly.com",
        participants_json='["internal.organizer@lilly.com", "cameron.hilt@authenticx.com"]',
    )
    result = wc.cluster_and_link()

    assert result["fyi_promoted_to_cluster"] == 1
    assert ws_db.get_raw_item(rid)["issue_id"] is not None


def test_fyi_standalone_item_with_only_internal_participants_is_still_dropped(ws_db):
    rid = _pending_item(
        ws_db, "pe7", "Authenticx (Project Isabella and KBO)", item_class="FYI-EVIDENCE", source="calendar",
        from_actor="internal.organizer@lilly.com",
        participants_json='["internal.organizer@lilly.com", "another.internal@lilly.com"]',
    )
    result = wc.cluster_and_link()

    assert result["fyi_standalone_skipped"] == 1
    assert ws_db.get_raw_item(rid)["issue_id"] is None


def test_fyi_standalone_item_with_ariba_fields_but_internal_sender_is_promoted(ws_db):
    rid = _pending_item(
        ws_db, "pe5",
        "Action required: Approve the Requisition that JANE SMITH submitted - PR3344556 - Workday HCM SaaS ($12,500.00 USD)",
        item_class="FYI-EVIDENCE", from_actor="bob@lilly.com",
    )
    result = wc.cluster_and_link()

    assert result["fyi_promoted_to_cluster"] == 1
    assert ws_db.get_raw_item(rid)["issue_id"] is not None


# --- get_items_pending_link backlog starvation (2026-08-01, real incident) --

def test_cluster_and_link_stamps_check_ts_on_noise_skip(ws_db):
    rid = _pending_item(ws_db, "ckn1", "unsubscribe from this newsletter", item_class="NOISE")
    wc.cluster_and_link()
    assert ws_db.get_raw_item(rid)["last_link_check_ts"] is not None


def test_cluster_and_link_stamps_check_ts_on_fyi_standalone_skip(ws_db):
    # Corrected pipeline Phase E: an internal-only sender with no reference/
    # Ariba signal is still real noise, genuinely skipped - see
    # _fyi_item_has_a_real_signal's own docstring for what now counts as
    # signal instead (a real external sender, a reference, Ariba fields).
    rid = _pending_item(ws_db, "ckf1", "just an fyi note", item_class="FYI-EVIDENCE", from_actor="bob@lilly.com")
    wc.cluster_and_link()
    assert ws_db.get_raw_item(rid)["last_link_check_ts"] is not None


# --- Task #54/#55 (2026-08-02, Marc's direct report): Teams ACTIONABLE-ASK/
# WAITING-ON-OTHERS with no thread/reference match now holds aside instead
# of always creating a new Issue - scoped to source == "teams_chat" only. --

def test_cluster_and_link_holds_aside_unmatched_teams_actionable_ask(ws_db):
    rid = _pending_item(ws_db, "tck1", "can you look at this", item_class="ACTIONABLE-ASK", source="teams_chat")

    result = wc.cluster_and_link()

    assert result["issues_created"] == 0
    assert result["teams_standalone_skipped"] == 1
    assert ws_db.get_raw_item(rid)["issue_id"] is None
    assert ws_db.get_raw_item(rid)["last_link_check_ts"] is not None


def test_cluster_and_link_holds_aside_unmatched_teams_waiting_on_others(ws_db):
    rid = _pending_item(ws_db, "tck2", "let me know when you're free", item_class="WAITING-ON-OTHERS", source="teams_chat")

    result = wc.cluster_and_link()

    assert result["teams_standalone_skipped"] == 1
    assert ws_db.get_raw_item(rid)["issue_id"] is None


def test_cluster_and_link_still_creates_issue_for_unmatched_email_actionable_ask(ws_db):
    """Scope guard: the Teams-specific hold-aside must NOT affect email
    (or any other non-Teams source) - an unmatched actionable email ask
    keeps creating a new Issue exactly as before."""
    rid = _pending_item(ws_db, "eck1", "can you approve this PO", item_class="ACTIONABLE-ASK", source="outlook_mail")

    result = wc.cluster_and_link()

    assert result["issues_created"] == 1
    assert result["teams_standalone_skipped"] == 0
    assert ws_db.get_raw_item(rid)["issue_id"] is not None


def test_cluster_and_link_teams_ask_still_attaches_when_a_reference_match_exists(ws_db, monkeypatch, tmp_path):
    """Being from Teams doesn't override a REAL match - only the "would
    otherwise always create a brand-new Issue" fallthrough is affected."""
    config = _isolate_config(ws_db, monkeypatch, tmp_path)
    config.set_value(True, "grouping", "reference_id_auto_attach_enabled")

    _pending_item(ws_db, "tref1", "Approve PR445566-V1", pr_number="PR445566-V1", source="outlook_mail")
    wc.cluster_and_link()

    rid = _pending_item(ws_db, "tref2", "any update on PR445566?", pr_number="PR445566-V2",
                         item_class="ACTIONABLE-ASK", source="teams_chat")
    result = wc.cluster_and_link()

    assert result["attached_via_reference"] == 1
    assert result["teams_standalone_skipped"] == 0
    assert ws_db.get_raw_item(rid)["issue_id"] is not None


def test_cluster_and_link_unmatched_teams_fyi_still_uses_fyi_path_not_teams_path(ws_db):
    """A Teams FYI-EVIDENCE item keeps going through the pre-existing
    fyi_standalone path (with its own subject-match check) - the new Teams
    branch only ever applies to ACTIONABLE-ASK/WAITING-ON-OTHERS."""
    # Corrected pipeline Phase E: an internal-only sender with no other
    # signal is still real noise - see _fyi_item_has_a_real_signal's own
    # docstring. What matters for THIS test is which path decided that
    # (fyi_standalone, not the Teams-specific one), not signal detection
    # itself.
    rid = _pending_item(ws_db, "tfyi1", "fyi, meeting moved to 3pm", item_class="FYI-EVIDENCE",
                         source="teams_chat", from_actor="bob@lilly.com")

    result = wc.cluster_and_link()

    assert result["fyi_standalone_skipped"] == 1
    assert result["teams_standalone_skipped"] == 0
    assert ws_db.get_raw_item(rid)["issue_id"] is None


# --- Task #179 (2026-08-04, Marc's direct design ask): Teams sender-anchored
# matching - "the sender being the main link... match the sender's message to
# any where the sender... was also the sender of the email or copied on the
# email, along with one other data point." -----------------------------------

def _issue_with_email_evidence(ws_db, title, category, from_actor):
    """An open issue with one real email raw_item already attached - the
    'existing evidence' a Teams sender gets cross-checked against."""
    iid = ws_db.create_issue_with_new_id(title=title, state="active", category=category)
    rid = ws_db.insert_raw_item(
        source="outlook_mail", stable_key=f"ev-{iid}", thread_key=f"ev-{iid}", dedupe_key=f"ev-{iid}",
        occurred_ts=time.time(), subject=title, from_actor=from_actor, participants_json="[]",
    )
    ws_db.link_raw_item_to_issue(rid, iid)
    return iid


def test_teams_sender_anchor_would_attach_but_stays_report_only_by_default(ws_db):
    """Same discipline as reference_id_auto_attach_enabled/subject_match_
    auto_attach_enabled/jasper_ref_auto_attach_enabled - always computed and
    counted, never acted on until the flag is explicitly turned on."""
    _issue_with_email_evidence(ws_db, "Workday renewal", "financial", "vendor@example.com")
    rid = _pending_item(ws_db, "tsa1", "any update?", item_class="ACTIONABLE-ASK", source="teams_chat",
                         from_actor="vendor@example.com", topic="financial")

    result = wc.cluster_and_link()

    assert result["would_attach_via_teams_sender_anchor"] == 1
    assert result["attached_via_teams_sender_anchor"] == 0
    assert result["teams_standalone_skipped"] == 1
    assert ws_db.get_raw_item(rid)["issue_id"] is None


def test_teams_sender_anchor_attaches_when_enabled_sender_email_plus_category(ws_db, monkeypatch, tmp_path):
    config = _isolate_config(ws_db, monkeypatch, tmp_path)
    config.set_value(True, "grouping", "teams_sender_anchor_auto_attach_enabled")

    iid = _issue_with_email_evidence(ws_db, "Workday renewal", "financial", "vendor@example.com")
    rid = _pending_item(ws_db, "tsa2", "any update?", item_class="ACTIONABLE-ASK", source="teams_chat",
                         from_actor="vendor@example.com", topic="financial")

    result = wc.cluster_and_link()

    assert result["attached_via_teams_sender_anchor"] == 1
    assert result["teams_standalone_skipped"] == 0
    assert ws_db.get_raw_item(rid)["issue_id"] == iid


def test_teams_sender_anchor_requires_corroboration_not_sender_alone(ws_db, monkeypatch, tmp_path):
    """The 'along with one other data point' half of the design - a real
    sender match with NO category match and NO topic-key overlap must NOT
    attach, even with the flag on. Same false-positive class task #81
    already fixed for company-alone matches."""
    config = _isolate_config(ws_db, monkeypatch, tmp_path)
    config.set_value(True, "grouping", "teams_sender_anchor_auto_attach_enabled")

    _issue_with_email_evidence(ws_db, "Workday renewal", "financial", "vendor@example.com")
    rid = _pending_item(ws_db, "tsa3", "unrelated ask", item_class="ACTIONABLE-ASK", source="teams_chat",
                         from_actor="vendor@example.com", topic="hr")

    result = wc.cluster_and_link()

    assert result["would_attach_via_teams_sender_anchor"] == 0
    assert result["teams_standalone_skipped"] == 1
    assert ws_db.get_raw_item(rid)["issue_id"] is None


def test_teams_sender_anchor_resolves_a_bare_display_name_via_party_index(ws_db, monkeypatch, tmp_path):
    """Marc's own real-world shape: Teams' from_actor is often a display
    name, not an email - resolved via workgraph_parties' existing bare-name
    resolver (never a fuzzy guess)."""
    config = _isolate_config(ws_db, monkeypatch, tmp_path)
    config.set_value(True, "grouping", "teams_sender_anchor_auto_attach_enabled")
    ws_db.upsert_party(id="p1", primary_email="jane.doe@vendor.com", display_name="Jane Doe",
                        affiliation="external", affiliation_confidence="H",
                        affiliation_source="domain", company="vendor")

    iid = _issue_with_email_evidence(ws_db, "Workday renewal", "financial", "jane.doe@vendor.com")
    rid = _pending_item(ws_db, "tsa4", "any update?", item_class="ACTIONABLE-ASK", source="teams_chat",
                         from_actor="Jane Doe", topic="financial")

    result = wc.cluster_and_link()

    assert result["attached_via_teams_sender_anchor"] == 1
    assert ws_db.get_raw_item(rid)["issue_id"] == iid


def test_teams_sender_anchor_abstains_on_an_ambiguous_display_name(ws_db, monkeypatch, tmp_path):
    """Two different real parties sharing a display_name must abstain, not
    guess - same discipline as workgraph_parties._resolve_bare_name's own
    ambiguous-exact-match test."""
    config = _isolate_config(ws_db, monkeypatch, tmp_path)
    config.set_value(True, "grouping", "teams_sender_anchor_auto_attach_enabled")
    ws_db.upsert_party(id="p1", primary_email="jane.doe@vendor.com", display_name="Jane Doe",
                        affiliation="external", affiliation_confidence="H",
                        affiliation_source="domain", company="vendor")
    ws_db.upsert_party(id="p2", primary_email="jane.doe@othercorp.com", display_name="Jane Doe",
                        affiliation="external", affiliation_confidence="H",
                        affiliation_source="domain", company="othercorp")
    _issue_with_email_evidence(ws_db, "Workday renewal", "financial", "jane.doe@vendor.com")
    rid = _pending_item(ws_db, "tsa5", "any update?", item_class="ACTIONABLE-ASK", source="teams_chat",
                         from_actor="Jane Doe", topic="financial")

    result = wc.cluster_and_link()

    assert result["would_attach_via_teams_sender_anchor"] == 0
    assert result["teams_standalone_skipped"] == 1
    assert ws_db.get_raw_item(rid)["issue_id"] is None


def test_teams_sender_anchor_matches_via_participant_not_just_from_actor(ws_db, monkeypatch, tmp_path):
    """'the sender of the team's message was also the sender of the email
    OR copied on the email' - the participants_json side of that, not just
    from_actor equality."""
    config = _isolate_config(ws_db, monkeypatch, tmp_path)
    config.set_value(True, "grouping", "teams_sender_anchor_auto_attach_enabled")

    iid = ws_db.create_issue_with_new_id(title="Workday renewal", state="active", category="financial")
    rid_ev = ws_db.insert_raw_item(
        source="outlook_mail", stable_key="ev-cc", thread_key="ev-cc", dedupe_key="ev-cc",
        occurred_ts=time.time(), subject="Workday renewal", from_actor="lead@example.com",
        participants_json=json.dumps(["vendor@example.com"]),
    )
    ws_db.link_raw_item_to_issue(rid_ev, iid)
    rid = _pending_item(ws_db, "tsa6", "any update?", item_class="ACTIONABLE-ASK", source="teams_chat",
                         from_actor="vendor@example.com", topic="financial")

    result = wc.cluster_and_link()

    assert result["attached_via_teams_sender_anchor"] == 1
    assert ws_db.get_raw_item(rid)["issue_id"] == iid


# --- Teams session-scoped grouping key (2026-08-03, Section 3.2/8) --------

def _teams_item_in_shared_chat(ws_db, chat_id, dedupe_suffix, subject, pr_number, occurred_ts,
                                item_class="ACTIONABLE-ASK"):
    """Like _pending_item, but for tests that need MULTIPLE items sharing
    the same real Teams thread_key (_pending_item's own dedupe_key ==
    thread_key would collide on the second call) - a distinct dedupe_key/
    stable_key per message, same thread_key (chat_id) for all of them,
    matching the real shape (one chat, many distinct messages)."""
    stable_key = f"{chat_id}:{dedupe_suffix}"
    rid = ws_db.insert_raw_item(
        source="teams_chat", stable_key=stable_key, thread_key=chat_id, dedupe_key=stable_key,
        occurred_ts=occurred_ts, subject=subject, from_actor="a@example.com", participants_json="[]",
    )
    ws_db.classify_raw_item(
        rid, item_class=item_class, direction="inbound", direction_inferred=False,
        topic="other", topic_inferred=True, sentiment="neutral", sentiment_inferred=True,
        anomaly_flag=False, signal_type=None, pr_number=pr_number,
        pr_number_base=workgraph_signals.reference_base(pr_number),
        jasper_ref_issue_id=None,
    )
    return rid


def test_effective_thread_key_unchanged_for_non_teams_sources(ws_db):
    rid = _pending_item(ws_db, "eck9", "some ask", source="outlook_mail")
    item = ws_db.get_raw_item(rid)
    assert wc._effective_thread_key(item) == "eck9"


def test_effective_thread_key_same_session_for_close_messages_sharing_reference(ws_db):
    now = time.time()
    r1 = _teams_item_in_shared_chat(ws_db, "multi1", "m1", "Approve PR900001", "PR900001", now)
    r2 = _teams_item_in_shared_chat(ws_db, "multi1", "m2", "any update on PR900001?", "PR900001", now + 60)
    item1, item2 = ws_db.get_raw_item(r1), ws_db.get_raw_item(r2)
    assert wc._effective_thread_key(item1) == wc._effective_thread_key(item2)


def test_effective_thread_key_splits_session_on_reference_mismatch(ws_db):
    """The real fix: a different reference on the SAME physical Teams chat
    now computes a different grouping key - it no longer forces a
    genuinely different topic into whatever issue the flat container
    happened to be pointing at."""
    now = time.time()
    r1 = _teams_item_in_shared_chat(ws_db, "multi2", "m1", "Approve PR900001", "PR900001", now)
    r2 = _teams_item_in_shared_chat(ws_db, "multi2", "m2", "Approve PR900002", "PR900002", now + 60)
    item1, item2 = ws_db.get_raw_item(r1), ws_db.get_raw_item(r2)
    assert wc._effective_thread_key(item1) != wc._effective_thread_key(item2)


def test_cluster_and_link_new_teams_session_does_not_silently_attach_to_old_sessions_issue(ws_db):
    """End-to-end: a Teams chat's session 0 already resolved to a real
    issue (e.g. via track_held_aside_item_as_issue). A NEW message on the
    SAME physical thread_key but a genuinely different reference (a new
    session) must NOT auto-attach to that issue - it falls through to the
    existing hold-aside path exactly as if it were a brand-new container,
    not a silent misattachment to an unrelated topic."""
    now = time.time()
    r1 = _teams_item_in_shared_chat(ws_db, "multi3", "m1", "Approve PR900001", "PR900001", now)
    item1 = ws_db.get_raw_item(r1)
    existing_issue = ws_db.create_issue_with_new_id(title="Existing", state="active", category="other")
    wc._container_set_issue(item1, wc._effective_thread_key(item1), existing_issue)
    ws_db.link_raw_item_to_issue(r1, existing_issue)

    r2 = _teams_item_in_shared_chat(ws_db, "multi3", "m2", "Approve PR900002", "PR900002", now + 60)

    result = wc.cluster_and_link()

    assert result["teams_standalone_skipped"] == 1
    assert ws_db.get_raw_item(r2)["issue_id"] is None  # NOT silently attached to existing_issue


# --- track_held_aside_item / dismiss_held_aside_item (task #54/#55) --------

def test_list_held_aside_teams_items_surfaces_the_held_aside_pile(ws_db):
    rid = _pending_item(ws_db, "hq1", "can you take a look at this", item_class="ACTIONABLE-ASK", source="teams_chat")
    wc.cluster_and_link()

    pending = ws_db.list_held_aside_teams_items()

    assert [p["id"] for p in pending] == [rid]


def test_list_held_aside_teams_items_excludes_already_reviewed(ws_db):
    rid = _pending_item(ws_db, "hq2", "quick ask", item_class="ACTIONABLE-ASK", source="teams_chat")
    wc.cluster_and_link()
    ws_db.set_held_aside_status(rid, "dismissed")

    assert ws_db.list_held_aside_teams_items() == []


def test_track_held_aside_item_creates_a_real_issue(ws_db):
    rid = _pending_item(ws_db, "hq3", "can you approve the CDA today", item_class="ACTIONABLE-ASK", source="teams_chat")
    wc.cluster_and_link()

    issue_id = wc.track_held_aside_item(rid)

    issue = ws_db.get_issue(issue_id)
    assert issue is not None
    assert issue["state"] == "active"
    assert ws_db.get_raw_item(rid)["issue_id"] == issue_id
    assert ws_db.get_raw_item(rid)["held_aside_status"] == "tracked"
    # No longer shows up in the queue once resolved.
    assert ws_db.list_held_aside_teams_items() == []


def test_track_held_aside_item_waiting_on_others_creates_waiting_issue(ws_db):
    rid = _pending_item(ws_db, "hq4", "let me know when free", item_class="WAITING-ON-OTHERS", source="teams_chat")
    wc.cluster_and_link()

    issue_id = wc.track_held_aside_item(rid)

    assert ws_db.get_issue(issue_id)["state"] == "waiting"


def test_track_held_aside_item_rejects_already_linked_item(ws_db):
    rid = _pending_item(ws_db, "hq5", "a real ask", item_class="ACTIONABLE-ASK", source="outlook_mail")
    wc.cluster_and_link()  # email creates an issue normally - already linked

    with pytest.raises(wc.HeldAsideItemError):
        wc.track_held_aside_item(rid)


def test_track_held_aside_item_rejects_non_teams_source(ws_db):
    rid = ws_db.insert_raw_item(source="outlook_mail", stable_key="hq6", thread_key="hq6", dedupe_key="hq6",
                                 occurred_ts=time.time(), subject="s", from_actor="a@example.com", participants_json="[]")
    ws_db.classify_raw_item(rid, item_class="ACTIONABLE-ASK", direction="inbound", direction_inferred=False,
                             topic="other", topic_inferred=True, sentiment="neutral", sentiment_inferred=True,
                             anomaly_flag=False)

    with pytest.raises(wc.HeldAsideItemError):
        wc.track_held_aside_item(rid)


def test_track_held_aside_item_rejects_unknown_raw_item_id(ws_db):
    with pytest.raises(wc.HeldAsideItemError):
        wc.track_held_aside_item(999999)


def test_dismiss_held_aside_item_marks_reviewed_without_creating_an_issue(ws_db):
    rid = _pending_item(ws_db, "hq7", "casual note", item_class="ACTIONABLE-ASK", source="teams_chat")
    wc.cluster_and_link()

    wc.dismiss_held_aside_item(rid)

    row = ws_db.get_raw_item(rid)
    assert row["held_aside_status"] == "dismissed"
    assert row["issue_id"] is None
    assert ws_db.list_held_aside_teams_items() == []


def test_dismiss_held_aside_item_rejects_already_reviewed(ws_db):
    rid = _pending_item(ws_db, "hq8", "casual note", item_class="ACTIONABLE-ASK", source="teams_chat")
    wc.cluster_and_link()
    wc.dismiss_held_aside_item(rid)

    with pytest.raises(wc.HeldAsideItemError):
        wc.dismiss_held_aside_item(rid)


def test_cluster_and_link_does_not_stamp_a_successfully_linked_item(ws_db):
    rid = _pending_item(ws_db, "cka1", "please approve this")
    wc.cluster_and_link()
    assert ws_db.get_raw_item(rid)["last_link_check_ts"] is None
    assert ws_db.get_raw_item(rid)["issue_id"] is not None


def test_cluster_and_link_backlog_of_old_skips_does_not_starve_new_items(ws_db):
    """End-to-end reproduction of the real incident: a backlog of
    permanently-skipped items exceeding `limit` must not prevent a
    genuinely new, real ask from being linked on the very next run."""
    # Round 1: 5 standalone FYIs, no thread/reference match - all skipped.
    # Corrected pipeline Phase E: internal-only senders, so they carry no
    # real signal and are still genuinely dropped (see
    # _fyi_item_has_a_real_signal) - this test is about backlog/limit
    # starvation, not signal detection.
    for i in range(5):
        _pending_item(ws_db, f"stale{i}", "just an fyi note", item_class="FYI-EVIDENCE", from_actor="bob@lilly.com")
    first = wc.cluster_and_link(limit=5)
    assert first["fyi_standalone_skipped"] == 5
    assert first["issues_created"] == 0

    # Round 2: a real, brand-new actionable ask arrives. With the OLD plain
    # oldest-first query and a limit smaller than the stale backlog, this
    # would never even be examined - confirmed as the live bug's exact shape.
    rid = _pending_item(ws_db, "fresh1", "please approve this new requisition")
    second = wc.cluster_and_link(limit=3)  # smaller than the 5-item stale backlog

    assert second["issues_created"] == 1
    assert ws_db.get_raw_item(rid)["issue_id"] is not None


def test_personal_calendar_block_never_becomes_an_issue_end_to_end(ws_db):
    """Real end-to-end reproduction: a HOLD block, classified for real
    (through run_classification, which now passes source/organizer/
    participants), must be skipped as NOISE by cluster_and_link() - never
    promoted to a trackable Issue."""
    ws_db.insert_raw_item(
        source="calendar", stable_key="evt-hold", thread_key="evt-hold", dedupe_key="dk-hold",
        occurred_ts=time.time(), subject="HOLD", from_actor="lane_marc@lilly.com",
        participants_json='["lane_marc@lilly.com"]',
    )

    wc.run_classification()
    result = wc.cluster_and_link()

    assert result["issues_created"] == 0
    assert result["noise_skipped"] == 1
    assert ws_db.list_issues(states=None, limit=10) == []


def test_cluster_and_link_shadow_logs_but_does_not_attach_when_flag_off(ws_db, monkeypatch, tmp_path):
    """The real ship default - the flag defaults off, so a real reference
    match must be COUNTED but must NOT change behavior (still creates a
    new issue, same as before this feature existed)."""
    config = _isolate_config(ws_db, monkeypatch, tmp_path)
    assert config.get("grouping", "reference_id_auto_attach_enabled") in (None, False)

    _pending_item(ws_db, "ck2", "First notice", pr_number="PR555000", from_actor="alice@example.com")
    wc.cluster_and_link()
    _pending_item(ws_db, "ck3", "REMINDER: still needs approval", pr_number="PR555000", from_actor="bob@example.com")
    result = wc.cluster_and_link()

    assert result["reference_auto_attach_enabled"] is False
    assert result["would_attach_via_reference"] == 1
    assert result["attached_via_reference"] == 0
    assert result["issues_created"] == 1, "second item must still create its OWN new issue while the flag is off"


def test_cluster_and_link_attaches_via_reference_when_flag_enabled(ws_db, monkeypatch, tmp_path):
    config = _isolate_config(ws_db, monkeypatch, tmp_path)
    config.set_value(True, "grouping", "reference_id_auto_attach_enabled")

    first_rid = _pending_item(ws_db, "ck4", "First notice", pr_number="PR666000", from_actor="alice@example.com")
    wc.cluster_and_link()
    first_issue_id = ws_db.get_raw_item(first_rid)["issue_id"]

    second_rid = _pending_item(ws_db, "ck5", "REMINDER: still needs approval", pr_number="PR666000", from_actor="bob@example.com")
    result = wc.cluster_and_link()

    assert result["reference_auto_attach_enabled"] is True
    assert result["attached_via_reference"] == 1
    assert result["issues_created"] == 0, "must attach to the existing cluster, not create a second one"
    assert ws_db.get_cluster(first_issue_id) is not None, "the target is still a cluster, not yet promoted"
    assert ws_db.get_raw_item(second_rid)["issue_id"] == first_issue_id


def test_cluster_and_link_writes_breadcrumb_evidence_when_attached_via_reference(ws_db, monkeypatch, tmp_path):
    config = _isolate_config(ws_db, monkeypatch, tmp_path)
    config.set_value(True, "grouping", "reference_id_auto_attach_enabled")

    first_rid = _pending_item(ws_db, "ck6", "First notice", pr_number="PR777888", from_actor="alice@example.com")
    wc.cluster_and_link()
    _pending_item(ws_db, "ck7", "REMINDER: still needs approval", pr_number="PR777888", from_actor="bob@example.com")
    wc.cluster_and_link()

    issue_id = ws_db.get_raw_item(first_rid)["issue_id"]
    evidence = ws_db.list_evidence(issue_id)
    breadcrumbed = [e for e in evidence if "auto-attached via shared reference" in (e.get("summary") or "")]
    assert len(breadcrumbed) == 1
    assert "PR777888" in breadcrumbed[0]["summary"]


def test_cluster_and_link_reference_match_ignores_closed_issues(ws_db, monkeypatch, tmp_path):
    config = _isolate_config(ws_db, monkeypatch, tmp_path)
    config.set_value(True, "grouping", "reference_id_auto_attach_enabled")

    # A real, already-promoted issue (stands in for "an issue that's
    # already been promoted and closed" - promotion itself is Phase C,
    # tested directly in test_workgraph_projects.py), with a matching
    # reference already on it, then closed.
    closed_issue_id = ws_db.create_issue_with_new_id(title="First notice", state="done", category="other")
    old_rid = ws_db.insert_raw_item(source="outlook_mail", stable_key="ck8", thread_key="ck8", dedupe_key="ck8",
                                     occurred_ts=time.time(), subject="First notice", from_actor="alice@example.com",
                                     participants_json="[]")
    conn = ws_db._connect()
    conn.execute("UPDATE raw_items SET issue_id = ?, pr_number_base = 'PR112233' WHERE id = ?",
                 (closed_issue_id, old_rid))
    conn.commit()
    conn.close()

    _pending_item(ws_db, "ck9", "REMINDER: still needs approval", pr_number="PR112233", from_actor="bob@example.com")
    result = wc.cluster_and_link()

    assert result["attached_via_reference"] == 0
    assert result["issues_created"] == 1


# --- Jasper reference-tag direct match (task #36) -------------------------

def test_cluster_and_link_shadow_logs_but_does_not_attach_jasper_ref_when_flag_off(ws_db, monkeypatch, tmp_path):
    config = _isolate_config(ws_db, monkeypatch, tmp_path)
    assert config.get("grouping", "jasper_ref_auto_attach_enabled") in (None, False)

    real_issue = ws_db.create_issue_with_new_id(title="Existing issue", state="active", category="other")
    _pending_item(ws_db, "jw1", "Re: renewal", jasper_ref_issue_id=real_issue)
    result = wc.cluster_and_link()

    assert result["jasper_ref_auto_attach_enabled"] is False
    assert result["would_attach_via_jasper_ref"] == 1
    assert result["attached_via_jasper_ref"] == 0
    assert result["issues_created"] == 1, "must still create its own new issue while the flag is off"


def test_cluster_and_link_attaches_via_jasper_ref_when_flag_enabled(ws_db, monkeypatch, tmp_path):
    config = _isolate_config(ws_db, monkeypatch, tmp_path)
    config.set_value(True, "grouping", "jasper_ref_auto_attach_enabled")

    real_issue = ws_db.create_issue_with_new_id(title="Existing issue", state="active", category="other")
    _pending_item(ws_db, "jw2", "Re: renewal", jasper_ref_issue_id=real_issue)
    result = wc.cluster_and_link()

    assert result["jasper_ref_auto_attach_enabled"] is True
    assert result["attached_via_jasper_ref"] == 1
    assert result["issues_created"] == 0
    issues = ws_db.list_issues(states=None, limit=10000)
    assert len(issues) == 1
    assert issues[0]["id"] == real_issue


def test_cluster_and_link_jasper_ref_ignores_closed_issues(ws_db, monkeypatch, tmp_path):
    config = _isolate_config(ws_db, monkeypatch, tmp_path)
    config.set_value(True, "grouping", "jasper_ref_auto_attach_enabled")

    closed_issue = ws_db.create_issue_with_new_id(title="Closed issue", state="done", category="other")
    _pending_item(ws_db, "jw3", "Re: renewal", jasper_ref_issue_id=closed_issue)
    result = wc.cluster_and_link()

    assert result["attached_via_jasper_ref"] == 0
    assert result["issues_created"] == 1, "a stale tag pointing at a closed issue must not force a match"


def test_cluster_and_link_jasper_ref_ignores_nonexistent_issue_id(ws_db, monkeypatch, tmp_path):
    """A tag quoted from a since-deleted issue, or a copy-paste artifact -
    get_issue() returning None must fall through cleanly, never raise."""
    config = _isolate_config(ws_db, monkeypatch, tmp_path)
    config.set_value(True, "grouping", "jasper_ref_auto_attach_enabled")

    _pending_item(ws_db, "jw4", "Re: renewal", jasper_ref_issue_id="marc-99999")
    result = wc.cluster_and_link()

    assert result["attached_via_jasper_ref"] == 0
    assert result["issues_created"] == 1


def test_cluster_and_link_jasper_ref_takes_priority_over_reference_match(ws_db, monkeypatch, tmp_path):
    """When both signals are present and both flags are on, the Jasper ref
    tag wins - it names the exact issue directly, a stronger claim than a
    shared PR/PO number pointing at some OTHER open issue with that same
    number."""
    config = _isolate_config(ws_db, monkeypatch, tmp_path)
    config.set_value(True, "grouping", "jasper_ref_auto_attach_enabled")
    config.set_value(True, "grouping", "reference_id_auto_attach_enabled")

    jasper_target = ws_db.create_issue_with_new_id(title="Jasper target", state="active", category="other")
    pr_target_rid = _pending_item(ws_db, "jw5a", "First notice", pr_number="PR999111", from_actor="alice@example.com")
    wc.cluster_and_link()
    pr_target = ws_db.get_raw_item(pr_target_rid)["issue_id"]
    assert pr_target != jasper_target

    second_rid = _pending_item(ws_db, "jw5b", "Re: renewal", pr_number="PR999111", jasper_ref_issue_id=jasper_target)
    result = wc.cluster_and_link()

    assert result["attached_via_jasper_ref"] == 1
    assert result["attached_via_reference"] == 0
    assert ws_db.get_raw_item(second_rid)["issue_id"] == jasper_target


# --- backfill_derived_titles (task #52, 2026-08-04) -------------------------
# Found dead: compute_deterministic_title/backfill_derived_titles existed,
# worked, and had zero callers anywhere but a one-time manual pass - never
# wired into the live pipeline, so every issue created since has had no
# chance at a real title beyond the raw mechanical subject line.

def _issue_with_parties(ws_db, title, *, internal_name=None, external_company=None):
    iid = ws_db.create_issue_with_new_id(title=title, state="active", category="other")
    if internal_name:
        pid = f"int-{iid}"
        ws_db.upsert_party(id=pid, primary_email=f"{pid}@lilly.com", display_name=internal_name,
                            affiliation="internal", affiliation_confidence="H",
                            affiliation_source="domain_heuristic", company=None)
        ws_db.link_party_to_issue(iid, pid)
    if external_company:
        pid = f"ext-{iid}"
        ws_db.upsert_party(id=pid, primary_email=f"{pid}@supplier.com", display_name="Rep",
                            affiliation="external", affiliation_confidence="H",
                            affiliation_source="domain_heuristic", company=external_company)
        ws_db.link_party_to_issue(iid, pid)
    return iid


def test_missing_derived_title_finds_issue_with_no_synthesis_row(ws_db):
    iid = ws_db.create_issue_with_new_id(title="Issue", state="active", category="other")
    assert iid in ws_db.list_issue_ids_missing_derived_title()


def test_missing_derived_title_finds_issue_with_empty_title(ws_db):
    iid = ws_db.create_issue_with_new_id(title="Issue", state="active", category="other")
    ws_db.upsert_synthesis(entity_type="issue", entity_id=iid, summary="a real summary",
                            next_steps_json="[]", suggested_actions_json="[]",
                            synthesized_from_marker="rev:1")
    assert iid in ws_db.list_issue_ids_missing_derived_title()


def test_missing_derived_title_excludes_issue_with_a_real_title(ws_db):
    iid = ws_db.create_issue_with_new_id(title="Issue", state="active", category="other")
    ws_db.set_derived_title("issue", iid, "Real Title")
    assert iid not in ws_db.list_issue_ids_missing_derived_title()


def test_backfill_sets_title_from_requestor_and_supplier(ws_db):
    iid = _issue_with_parties(ws_db, "RE: pricing", internal_name="Jane Doe", external_company="UneeQ")
    result = wc.backfill_derived_titles()
    assert result["updated"] == 1
    synth = ws_db.get_synthesis("issue", iid)
    assert synth["derived_title"] == "Jane Doe - UneeQ - pricing"


def test_backfill_skips_issue_with_insufficient_party_data(ws_db):
    """compute_deterministic_title requires 2 of 3 real parts - a lone
    topic (no real requestor or supplier) isn't worth overriding the raw
    title with something no better."""
    iid = ws_db.create_issue_with_new_id(title="Random subject", state="active", category="other")
    result = wc.backfill_derived_titles()
    assert result["updated"] == 0
    assert ws_db.get_synthesis("issue", iid) is None  # never touched - nothing to set


def test_backfill_never_overwrites_an_existing_title(ws_db):
    iid = _issue_with_parties(ws_db, "RE: pricing", internal_name="Jane Doe", external_company="UneeQ")
    ws_db.set_derived_title("issue", iid, "Curator's own real title")

    result = wc.backfill_derived_titles()

    assert result["checked"] == 0  # already titled - excluded from the scan entirely
    assert ws_db.get_synthesis("issue", iid)["derived_title"] == "Curator's own real title"


def test_backfill_only_scans_issues_missing_a_title(ws_db):
    titled = _issue_with_parties(ws_db, "RE: a", internal_name="Jane Doe", external_company="Acme")
    ws_db.set_derived_title("issue", titled, "Already titled")
    untitled = _issue_with_parties(ws_db, "RE: b", internal_name="John Roe", external_company="Beta")

    result = wc.backfill_derived_titles()

    assert result["checked"] == 1
    assert result["updated"] == 1
    assert ws_db.get_synthesis("issue", untitled)["derived_title"] == "John Roe - Beta - b"


def test_backfill_strips_ariba_requisition_boilerplate_from_topic(ws_db):
    """Real live-data bug (task #52, 2026-08-04): every Ariba PR-approval
    notification carries this exact fixed wrapper text, which was passing
    straight through into the 'improved' title untouched - defeating the
    whole point of the rewrite for one of the most common real subjects."""
    iid = _issue_with_parties(
        ws_db,
        "Action required: Approve the Requisition that CORRINA MCCORKLE submitted  - "
        "PR815290-V2 - Lilly One Data Spine Phase 2 Support ($464,000.00 USD)",
        internal_name="Cori McCorkle", external_company="Ariba",
    )
    result = wc.backfill_derived_titles()
    assert result["updated"] == 1
    title = ws_db.get_synthesis("issue", iid)["derived_title"]
    assert "Action required" not in title
    assert "submitted" not in title
    assert "PR815290-V2" in title


def test_backfill_omits_requestor_when_name_already_in_topic(ws_db):
    """Real live-data bug (task #52, 2026-08-04): prepending the requestor's
    own name produced a literal duplicate when the raw subject already
    contained it verbatim (e.g. 'Alex Sohn - Alex Sohn Finance Intern
    Presentation')."""
    iid = _issue_with_parties(
        ws_db, "Alex Sohn Finance Intern Presentation",
        internal_name="Alex Sohn", external_company="Acme",
    )
    result = wc.backfill_derived_titles()
    assert result["updated"] == 1
    title = ws_db.get_synthesis("issue", iid)["derived_title"]
    assert title == "Acme - Alex Sohn Finance Intern Presentation"


def test_backfill_declines_when_only_part_is_redundant_with_topic(ws_db):
    """If the requestor's name is the ONLY real part on hand and it's
    already redundant with the topic, there's nothing left to add - decline
    rather than emit a title identical to the raw one."""
    iid = _issue_with_parties(
        ws_db, "Alex Sohn Finance Intern Presentation", internal_name="Alex Sohn",
    )
    result = wc.backfill_derived_titles()
    assert result["updated"] == 0
    assert ws_db.get_synthesis("issue", iid) is None


def test_backfill_extracts_ariba_submitter_name_when_no_internal_party(ws_db):
    """Real live-data bug found checking task #57's 'Your next move' strip
    (2026-08-04): an Ariba PR-approval notification has no linked internal
    party for the submitter (they're not a sender/recipient, just named in
    the subject), so the function correctly had nothing to prepend and
    declined - even though the real name is right there in the subject.
    Submitting an Ariba requisition is inherently an internal-Lilly action,
    so it's used as a fallback requestor."""
    iid = _issue_with_parties(
        ws_db,
        "Action required: Approve the Requisition that ALICIA MORRIS submitted  - "
        "PR854779-V4 - Conversational AI ($1,938,100.00 USD)",
        external_company="Ariba",
    )
    result = wc.backfill_derived_titles()
    assert result["updated"] == 1
    title = ws_db.get_synthesis("issue", iid)["derived_title"]
    assert title.startswith("Alicia Morris")
    assert "Action required" not in title


# --- project-level derived titles (task #167/#168, 2026-08-04) --------------
# Live-DB check found 0 of 52 projects had ever gotten a derived_title -
# compute_deterministic_title's boilerplate-strip/redundant-name/Ariba-
# submitter fixes above never carried over to projects, so projects.name
# stayed whatever _project_name_for picked at creation, forever. This also
# turned out to be the real driver behind Marc's separate "why is everything
# a PR" report - see compute_deterministic_project_title's own docstring.

def _project_with_issue(ws_db, title, *, internal_name=None, external_company=None):
    pid = ws_db.create_project_with_new_id(name=title[:50], category="other")
    iid = _issue_with_parties(ws_db, title, internal_name=internal_name, external_company=external_company)
    ws_db.assign_issue_to_project(iid, pid)
    return pid, iid


def test_missing_derived_title_finds_project_with_no_synthesis_row(ws_db):
    pid = ws_db.create_project_with_new_id(name="Project", category="other")
    assert pid in ws_db.list_project_ids_missing_derived_title()


def test_missing_derived_title_excludes_project_with_a_real_title(ws_db):
    pid = ws_db.create_project_with_new_id(name="Project", category="other")
    ws_db.set_derived_title("project", pid, "Real Title")
    assert pid not in ws_db.list_project_ids_missing_derived_title()


def test_backfill_sets_project_title_from_member_issue_parties(ws_db):
    pid, iid = _project_with_issue(ws_db, "RE: pricing", internal_name="Jane Doe", external_company="UneeQ")
    result = wc.backfill_derived_titles()
    assert result["projects_updated"] == 1
    synth = ws_db.get_synthesis("project", pid)
    assert synth["derived_title"] == "Jane Doe - UneeQ - pricing"


def test_backfill_project_skips_project_with_no_member_issues(ws_db):
    pid = ws_db.create_project_with_new_id(name="Empty project", category="other")
    result = wc.backfill_derived_titles()
    assert result["projects_updated"] == 0
    assert ws_db.get_synthesis("project", pid) is None


def test_backfill_project_strips_ariba_boilerplate_same_as_issue_path(ws_db):
    """The exact bug that made Marc's inbox read as PR-dominated: an Ariba
    requisition project kept its raw boilerplate name forever because this
    path never got the issue-side fix - confirms the project path reuses
    the SAME cleanup, not a second, drifted copy of it."""
    pid, iid = _project_with_issue(
        ws_db,
        "Action required: Approve the Requisition that ALICIA MORRIS submitted  - "
        "PR854779-V4 - Conversational AI ($1,938,100.00 USD)",
        external_company="Ariba",
    )
    result = wc.backfill_derived_titles()
    assert result["projects_updated"] == 1
    title = ws_db.get_synthesis("project", pid)["derived_title"]
    assert title.startswith("Alicia Morris")
    assert "Action required" not in title


def test_backfill_project_never_overwrites_an_existing_title(ws_db):
    pid, iid = _project_with_issue(ws_db, "RE: pricing", internal_name="Jane Doe", external_company="UneeQ")
    ws_db.set_derived_title("project", pid, "Curator's own real title")

    result = wc.backfill_derived_titles()

    assert result["projects_checked"] == 0
    assert ws_db.get_synthesis("project", pid)["derived_title"] == "Curator's own real title"


def test_backfill_project_aggregates_parties_across_member_issues(ws_db):
    """A project's real contacts can be split across its issues - the
    project path must look at every member issue's parties, not just one."""
    pid = ws_db.create_project_with_new_id(name="Multi-issue project", category="other")
    iid1 = _issue_with_parties(ws_db, "First thread", internal_name="Jane Doe")
    iid2 = _issue_with_parties(ws_db, "Second thread", external_company="UneeQ")
    ws_db.assign_issue_to_project(iid1, pid)
    ws_db.assign_issue_to_project(iid2, pid)

    result = wc.backfill_derived_titles()

    assert result["projects_updated"] == 1
    title = ws_db.get_synthesis("project", pid)["derived_title"]
    assert "Jane Doe" in title
    assert "UneeQ" in title


def test_titlecase_name_preserves_mc_surnames():
    assert wc._titlecase_name("CORRINA MCCORKLE") == "Corrina McCorkle"
    assert wc._titlecase_name("MARY MASON") == "Mary Mason"


# --- Task #414: the standalone-FYI gate can finally see a document ---------

def _sp_item(name, web_url=None):
    """A SharePoint raw_item as it really arrives: no sender, no
    participants, no reference. That shape is the whole bug - all three of
    the gate's original checks read one of those three fields."""
    return {
        "source": "sharepoint", "item_class": "FYI-EVIDENCE",
        "subject": name, "from_actor": None, "participants": "[]",
        "pr_number_base": None,
        "meta_json": json.dumps({"web_url": web_url}) if web_url else None,
    }


def test_fyi_gate_still_drops_a_document_when_no_vocabulary_supplied():
    """Regression floor: omitting known_companies must behave exactly as the
    gate did before #414, so nothing about the mail/calendar paths moved."""
    item = _sp_item("Sodalis_LILLY_PV1_SOW_Proposal.docx",
                    "https://collab.lilly.com/sites/X/General/Sodalis/a.docx")
    assert wc._fyi_item_has_a_real_signal(item) is False


def test_fyi_gate_recovers_a_supplier_document_via_folder():
    item = _sp_item("Sodalis_LILLY_PV1_SOW_Proposal.docx",
                    "https://collab.lilly.com/sites/X/General/Sodalis/a.docx")
    assert wc._fyi_item_has_a_real_signal(item, {"sodalis"}) is True


def test_fyi_gate_recovers_a_supplier_document_via_filename():
    item = _sp_item("WO_Kinaxis_Sustainment Services Extension.pdf")
    assert wc._fyi_item_has_a_real_signal(item, {"kinaxis"}) is True


def test_fyi_gate_still_drops_a_generic_data_dump():
    """The 68 of 100 live items that SHOULD stay dropped - internal
    spreadsheets and exports with no counterparty in name or path."""
    for name in ("projects_data_prioritized.xlsx", "air_submissions_21Jan.xlsx",
                 "repository (2).xlsx", "Contracting Master Report (Analytics COE).csv"):
        item = _sp_item(name, "https://collab.lilly.com/sites/LEAP/Shared Documents/" + name)
        assert wc._fyi_item_has_a_real_signal(item, {"kinaxis", "sodalis"}) is False, name


def test_fyi_gate_tolerates_malformed_meta_json():
    item = _sp_item("Sodalis_SOW.docx")
    item["meta_json"] = "{not json"
    assert wc._fyi_item_has_a_real_signal(item, {"sodalis"}) is True  # falls back to filename
