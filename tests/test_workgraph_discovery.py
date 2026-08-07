"""Tests for workgraph_discovery.py (personalized data-point discovery,
docs/design/PERSONALIZED_DATA_POINT_DISCOVERY.md, tasks #212-217).

Deliberately does NOT exercise propose_from_observation/llm_backfill_
missing_values against a real `claude -p` subprocess here - those were
verified manually against the live DB (see session notes), and a real
LLM call is too slow/costly/flaky for the automated suite. Everything
here is the deterministic half: signature derivation, the significance
bar, the fast-track seed, and the grouping retrofit's matching logic."""
import time

import workgraph_discovery as wd


def _item(id=1, from_actor="jane@vendor.com", thread_key="t1", subject="Subject",
          participants_json="[]", occurred_ts=None):
    return {
        "id": id, "from_actor": from_actor, "thread_key": thread_key, "subject": subject,
        "participants": participants_json, "body_preview": "", "raw_ref": None,
        "occurred_ts": occurred_ts if occurred_ts is not None else time.time(),
    }


# --- derive_pattern_signatures ----------------------------------------------

def test_sender_domain_signature_extracted():
    sigs = wd.derive_pattern_signatures(_item(from_actor="jane@newvendor.com"))
    assert "sender_domain:newvendor.com" in sigs


def test_internal_root_domain_excluded_but_subdomain_kept():
    sigs = wd.derive_pattern_signatures(_item(from_actor="colleague@lilly.com"))
    assert not any(s.startswith("sender_domain:") for s in sigs)
    sigs2 = wd.derive_pattern_signatures(_item(from_actor="it@network.lilly.com"))
    assert "sender_domain:network.lilly.com" in sigs2


def test_labeled_field_signature_extracted(monkeypatch):
    monkeypatch.setattr(wd.text_extract, "resolve_item_text", lambda item: "Batch Number: BN12345\nOther text.")
    sigs = wd.derive_pattern_signatures(_item())
    assert "labeled_field:batch number" in sigs


def test_boilerplate_labels_filtered_out(monkeypatch):
    monkeypatch.setattr(
        wd.text_extract, "resolve_item_text",
        lambda item: "Confidentiality Notice: this email...\nPhone: 555-1234\nSubject: hi",
    )
    sigs = wd.derive_pattern_signatures(_item())
    assert not any(s.startswith("labeled_field:") for s in sigs)


# --- significance bar --------------------------------------------------------

def test_crosses_significance_bar_requires_all_three_conditions():
    now = time.time()
    base = {"pattern_signature": "sender_domain:x.com", "first_seen_ts": now, "last_seen_ts": now,
            "promoted_to_definition_id": None}

    too_few_occurrences = {**base, "occurrence_count": 4, "distinct_thread_count": 2}
    assert wd.crosses_significance_bar(too_few_occurrences) is False

    too_few_threads = {**base, "occurrence_count": 10, "distinct_thread_count": 1}
    assert wd.crosses_significance_bar(too_few_threads) is False

    too_wide_a_window = {**base, "occurrence_count": 5, "distinct_thread_count": 2,
                          "last_seen_ts": now, "first_seen_ts": now - 61 * 86400}
    assert wd.crosses_significance_bar(too_wide_a_window) is False

    already_promoted = {**base, "occurrence_count": 10, "distinct_thread_count": 5,
                         "promoted_to_definition_id": "dp-already"}
    assert wd.crosses_significance_bar(already_promoted) is False

    real_crossing = {**base, "occurrence_count": 5, "distinct_thread_count": 2}
    assert wd.crosses_significance_bar(real_crossing) is True


def test_observation_thread_tracking_only_counts_genuinely_distinct_threads(ws_db):
    # Same thread repeated - occurrence_count grows, distinct_thread_count does not.
    ws_db.observe_candidate_pattern("sender_domain:x.com", is_new_thread=ws_db.record_pattern_observation_thread(
        "sender_domain:x.com", "thread-1"))
    for _ in range(4):
        is_new = ws_db.record_pattern_observation_thread("sender_domain:x.com", "thread-1")
        ws_db.observe_candidate_pattern("sender_domain:x.com", is_new_thread=is_new)
    row = ws_db.get_candidate_pattern_observation("sender_domain:x.com")
    assert row["occurrence_count"] == 5
    assert row["distinct_thread_count"] == 1
    assert wd.crosses_significance_bar(row) is False  # only 1 distinct thread

    # A genuinely new thread pushes it over the 2-thread bar.
    is_new = ws_db.record_pattern_observation_thread("sender_domain:x.com", "thread-2")
    ws_db.observe_candidate_pattern("sender_domain:x.com", is_new_thread=is_new)
    row2 = ws_db.get_candidate_pattern_observation("sender_domain:x.com")
    assert row2["distinct_thread_count"] == 2
    assert wd.crosses_significance_bar(row2) is True


# --- fast-track seed (#217) -------------------------------------------------

def test_seed_fasttrack_vocabulary_creates_seven_confirmed_points(ws_db):
    created = wd.seed_fasttrack_vocabulary(confirmed_by="marc")
    assert len(created) == 7
    confirmed = ws_db.list_data_point_definitions(status="confirmed")
    assert len(confirmed) == 7
    assert {d["point_type"] for d in confirmed} == {"entity", "person", "reference", "amount", "freetext", "date"}


def test_seed_fasttrack_vocabulary_is_idempotent(ws_db):
    wd.seed_fasttrack_vocabulary(confirmed_by="marc")
    second_call = wd.seed_fasttrack_vocabulary(confirmed_by="marc")
    assert second_call == []  # nothing new to create the second time
    assert len(ws_db.list_data_point_definitions(status="confirmed")) == 7


# --- matched_discovered_points retrofit (#215/#216) -------------------------

def test_matched_discovered_points_is_a_noop_with_only_fasttrack_definitions(ws_db):
    wd.seed_fasttrack_vocabulary(confirmed_by="marc")
    assert wd.matched_discovered_points("issue-a", "issue-b") == []


def test_matched_discovered_points_detects_a_real_shared_sender_domain(ws_db):
    ws_db.insert_raw_item(source="outlook_mail", stable_key="s1", thread_key="t1", dedupe_key="d1",
                           occurred_ts=time.time(), from_actor="rep@newvendor.com", subject="Hello")
    ws_db.insert_raw_item(source="outlook_mail", stable_key="s2", thread_key="t2", dedupe_key="d2",
                           occurred_ts=time.time(), from_actor="rep2@newvendor.com", subject="Follow-up")
    ws_db.link_raw_item_to_issue(1, "issue-a")
    ws_db.link_raw_item_to_issue(2, "issue-b")

    ws_db.observe_candidate_pattern("sender_domain:newvendor.com", is_new_thread=True)
    ws_db.create_data_point_definition(
        id="dp-newvendor", name="NewVendor", description="test", point_type="entity",
        deterministic_rule=None, discovered_from="test", status="confirmed",
    )
    ws_db.confirm_data_point_definition("dp-newvendor", confirmed_by="test")
    ws_db.mark_candidate_pattern_promoted("sender_domain:newvendor.com", "dp-newvendor")

    points = wd.matched_discovered_points("issue-a", "issue-b")
    assert points == ["discovered:dp-newvendor"]

    values = ws_db.list_data_point_values_for_work_object("issue-a")
    assert any(v["definition_id"] == "dp-newvendor" and v["value"] == "newvendor.com" for v in values)


def test_matched_discovered_points_no_match_when_domains_differ(ws_db):
    ws_db.insert_raw_item(source="outlook_mail", stable_key="s1", thread_key="t1", dedupe_key="d1",
                           occurred_ts=time.time(), from_actor="rep@vendor-a.com", subject="Hello")
    ws_db.insert_raw_item(source="outlook_mail", stable_key="s2", thread_key="t2", dedupe_key="d2",
                           occurred_ts=time.time(), from_actor="rep@vendor-b.com", subject="Follow-up")
    ws_db.link_raw_item_to_issue(1, "issue-a")
    ws_db.link_raw_item_to_issue(2, "issue-b")

    ws_db.observe_candidate_pattern("sender_domain:vendor-a.com", is_new_thread=True)
    ws_db.create_data_point_definition(
        id="dp-vendor-a", name="Vendor A", description="test", point_type="entity",
        deterministic_rule=None, discovered_from="test", status="confirmed",
    )
    ws_db.confirm_data_point_definition("dp-vendor-a", confirmed_by="test")
    ws_db.mark_candidate_pattern_promoted("sender_domain:vendor-a.com", "dp-vendor-a")

    assert wd.matched_discovered_points("issue-a", "issue-b") == []


# --- value_for_signature -----------------------------------------------------

def test_value_for_signature_sender_domain():
    item = _item(from_actor="rep@acme.com")
    assert wd.value_for_signature("sender_domain:acme.com", item) == "acme.com"
    assert wd.value_for_signature("sender_domain:other.com", item) is None


def test_value_for_signature_labeled_field(monkeypatch):
    monkeypatch.setattr(wd.text_extract, "resolve_item_text", lambda item: "Batch Number: BN99887\n")
    item = _item()
    assert wd.value_for_signature("labeled_field:batch number", item) == "BN99887"
    assert wd.value_for_signature("labeled_field:cost center", item) is None
