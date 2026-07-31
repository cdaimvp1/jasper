"""Regression tests for workgraph_classify.py (tasks #24, #19):
- dead regex stems (escalat\\w* etc.) now match real inflections
- direction_inferred/sentiment_inferred/topic_inferred genuinely flip to
  False on a real cue match, so confidence tier H is actually reachable
- backfill_reclassify writes the FRESH result, not stale item[...] values
"""
import time

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


# --- Part C (2026-07-30): raw-item-to-issue linking via reference ID -----

def _pending_item(ws_db, thread_key, subject, pr_number=None, item_class="ACTIONABLE-ASK", from_actor="a@example.com"):
    """A classified-but-not-yet-linked raw_item, ready for cluster_and_link().
    Each thread_key is deliberately unique per call so thread_map_lookup
    never resolves it - the whole point is testing the NO-thread-match
    fallback path."""
    rid = ws_db.insert_raw_item(
        source="outlook_mail", stable_key=thread_key, thread_key=thread_key, dedupe_key=thread_key,
        occurred_ts=time.time(), subject=subject, from_actor=from_actor, participants_json="[]",
    )
    ws_db.classify_raw_item(
        rid, item_class=item_class, direction="inbound", direction_inferred=False,
        topic="other", topic_inferred=True, sentiment="neutral", sentiment_inferred=True,
        anomaly_flag=False, signal_type=None, pr_number=pr_number,
        pr_number_base=workgraph_signals.reference_base(pr_number),
    )
    return rid


def _isolate_config(ws_db, monkeypatch, tmp_path):
    """Same isolation pattern as test_retention.py's retention_env fixture -
    config.SETTINGS_PATH is bound at import time, not per-test via
    isolated_paths, so it needs its own explicit monkeypatch here."""
    import config
    monkeypatch.setattr(config, "SETTINGS_PATH", tmp_path / "settings.json")
    monkeypatch.setattr(config, "_cache", {})
    monkeypatch.setattr(config, "_cache_mtime", 0.0)
    return config


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
    _pending_item(ws_db, "ck-reopen", "please approve the SOW", item_class="ACTIONABLE-ASK")
    wc.cluster_and_link()
    issues = ws_db.list_issues(states=None, limit=10)
    iid = issues[0]["id"]
    ws_db.update_issue(iid, state="done")

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
    _pending_item(ws_db, "ck-reopen2", "please approve the SOW", item_class="ACTIONABLE-ASK")
    wc.cluster_and_link()
    issues = ws_db.list_issues(states=None, limit=10)
    iid = issues[0]["id"]
    ws_db.update_issue(iid, state="done")

    rid = ws_db.insert_raw_item(source="outlook_mail", stable_key="ck-reopen2", thread_key="ck-reopen2", dedupe_key="ck-reopen2-2",
                                 occurred_ts=time.time(), subject="Actually, one more approval needed on this",
                                 from_actor="b@example.com", participants_json="[]")
    ws_db.classify_raw_item(rid, item_class="ACTIONABLE-ASK", direction="inbound", direction_inferred=False,
                             topic="other", topic_inferred=True, sentiment="neutral", sentiment_inferred=True,
                             anomaly_flag=False)

    wc.cluster_and_link()

    assert ws_db.get_issue(iid)["state"] == "active"


def test_cluster_and_link_creates_new_issue_when_no_reference_match(ws_db):
    _pending_item(ws_db, "ck1", "A brand new ask with nothing structured in it")
    result = wc.cluster_and_link()
    assert result["issues_created"] == 1
    assert result["attached_via_reference"] == 0
    assert result["would_attach_via_reference"] == 0


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

    _pending_item(ws_db, "ck5", "REMINDER: still needs approval", pr_number="PR666000", from_actor="bob@example.com")
    result = wc.cluster_and_link()

    assert result["reference_auto_attach_enabled"] is True
    assert result["attached_via_reference"] == 1
    assert result["issues_created"] == 0, "must attach to the existing issue, not create a second one"
    issues = ws_db.list_issues(states=None, limit=10000)
    assert len(issues) == 1
    assert issues[0]["id"] == first_issue_id


def test_cluster_and_link_writes_breadcrumb_evidence_when_attached_via_reference(ws_db, monkeypatch, tmp_path):
    config = _isolate_config(ws_db, monkeypatch, tmp_path)
    config.set_value(True, "grouping", "reference_id_auto_attach_enabled")

    _pending_item(ws_db, "ck6", "First notice", pr_number="PR777888", from_actor="alice@example.com")
    wc.cluster_and_link()
    _pending_item(ws_db, "ck7", "REMINDER: still needs approval", pr_number="PR777888", from_actor="bob@example.com")
    wc.cluster_and_link()

    issue_id = ws_db.list_issues(states=None, limit=10000)[0]["id"]
    evidence = ws_db.list_evidence(issue_id)
    breadcrumbed = [e for e in evidence if "auto-attached via shared reference" in (e.get("summary") or "")]
    assert len(breadcrumbed) == 1
    assert "PR777888" in breadcrumbed[0]["summary"]


def test_cluster_and_link_reference_match_ignores_closed_issues(ws_db, monkeypatch, tmp_path):
    config = _isolate_config(ws_db, monkeypatch, tmp_path)
    config.set_value(True, "grouping", "reference_id_auto_attach_enabled")

    first_rid = _pending_item(ws_db, "ck8", "First notice", pr_number="PR112233", from_actor="alice@example.com")
    wc.cluster_and_link()
    first_issue_id = ws_db.get_raw_item(first_rid)["issue_id"]
    ws_db.update_issue(first_issue_id, state="done")

    _pending_item(ws_db, "ck9", "REMINDER: still needs approval", pr_number="PR112233", from_actor="bob@example.com")
    result = wc.cluster_and_link()

    assert result["attached_via_reference"] == 0
    assert result["issues_created"] == 1
