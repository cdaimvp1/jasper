"""Tests for workgraph_synthesis_light.py (task #247's hybrid synthesis
routing, light half). Never invokes a real `claude -p` subprocess -
_run_headless_claude is monkeypatched throughout, same discipline as
test_workgraph_pipeline2.py's LLM-call tests."""
from __future__ import annotations

import json
import time

import workgraph_synthesis_light as wsl


class _FakeProc:
    def __init__(self, stdout):
        self.stdout = stdout
        self.stderr = ""


def _issue(ws_db, title="Issue"):
    return ws_db.create_issue_with_new_id(title=title, state="active", category="other")


def _cluster(ws_db, title="Cluster"):
    return ws_db.create_cluster_with_new_id(title=title, category="other")


def _raw_item(ws_db, issue_id, key, body, extracted=False):
    rid = ws_db.insert_raw_item(
        source="outlook_mail", stable_key=key, thread_key=key, dedupe_key=key,
        occurred_ts=time.time(), subject="s", from_actor="a@example.com",
        participants_json="[]", body_preview=body,
    )
    ws_db.link_raw_item_to_issue(rid, issue_id)
    if extracted:
        ws_db.create_extraction(rid, json.dumps({"asks": ["already handled"]}))
    return rid


_LIGHT_REPLY = json.dumps({
    "extractions": {},  # filled in per-test with the real raw_item id
    "synthesis": {"summary": "Vendor confirmed pricing.", "derived_title": "Pricing confirmed",
                  "next_steps": [{"step": "send PO", "current": True}],
                  "suggested_actions": [{"task_id": None, "label": "Send PO", "rationale": "pricing is final"}]},
})


# --- compute_new_evidence_bytes ---------------------------------------------

def test_compute_new_evidence_bytes_zero_when_nothing_new(ws_db):
    iid = _issue(ws_db)
    assert wsl.compute_new_evidence_bytes("issue", iid) == 0


def test_compute_new_evidence_bytes_counts_only_unextracted_items(ws_db):
    iid = _issue(ws_db)
    _raw_item(ws_db, iid, "k1", "already processed body", extracted=True)
    _raw_item(ws_db, iid, "k2", "brand new short body")
    size = wsl.compute_new_evidence_bytes("issue", iid)
    assert size > 0
    assert size < 200  # one short new item, nowhere near the 100KB threshold


def test_compute_new_evidence_bytes_aggregates_across_project_members(ws_db):
    pid = ws_db.create_project_with_new_id(name="Proj", category="other")
    iid = _issue(ws_db, "Member issue")
    ws_db.assign_issue_to_project(iid, pid, reason="test")
    cid = _cluster(ws_db, "Member cluster")
    ws_db.assign_issue_to_project(cid, pid, reason="test")
    _raw_item(ws_db, iid, "k1", "issue body text")
    size_issue_only = wsl.compute_new_evidence_bytes("issue", iid)
    size_project = wsl.compute_new_evidence_bytes("project", pid)
    assert size_project >= size_issue_only > 0


# --- run_light_synthesis -----------------------------------------------------

def test_run_light_synthesis_not_found_for_missing_issue():
    result = wsl.run_light_synthesis("issue", "no-such-issue")
    assert result["action"] == "not_found"


def test_run_light_synthesis_no_new_evidence_when_everything_already_extracted(ws_db):
    iid = _issue(ws_db)
    _raw_item(ws_db, iid, "k1", "old body", extracted=True)
    result = wsl.run_light_synthesis("issue", iid)
    assert result["action"] == "no_new_evidence"


def test_run_light_synthesis_writes_extraction_and_synthesis(ws_db, monkeypatch):
    iid = _issue(ws_db, "Vendor pricing thread")
    rid = _raw_item(ws_db, iid, "k1", "We confirm the $40,000 pricing is final.")

    reply = json.loads(_LIGHT_REPLY)
    reply["extractions"][str(rid)] = {
        "asks": [], "decisions": ["pricing is final"], "dates_mentioned": [],
        "commitments": [], "key_facts": ["$40,000 confirmed"],
    }
    monkeypatch.setattr(wsl, "_run_headless_claude", lambda prompt, timeout: _FakeProc(json.dumps(reply)))

    result = wsl.run_light_synthesis("issue", iid)

    assert result["action"] == "synthesized_light"
    assert result["extracted"] == 1

    extraction = ws_db.get_extraction(rid)
    assert extraction is not None
    assert extraction["extracted_json"]["decisions"] == ["pricing is final"]

    synthesis = ws_db.get_synthesis("issue", iid)
    assert synthesis["summary"] == "Vendor confirmed pricing."
    assert synthesis["derived_title"] == "Pricing confirmed"
    assert len(synthesis["next_steps"]) == 1

    # Marker written matches the entity's CURRENT (post-materialization)
    # revision - the next list_stale_entities() pass must see this as fresh.
    import workgraph_synthesis as wsyn
    assert synthesis["synthesized_from_marker"] == wsyn.compute_evidence_marker("issue", iid)


def test_run_light_synthesis_unparseable_output_makes_no_writes(ws_db, monkeypatch):
    iid = _issue(ws_db)
    rid = _raw_item(ws_db, iid, "k1", "some new content")
    monkeypatch.setattr(wsl, "_run_headless_claude", lambda prompt, timeout: _FakeProc("not json at all"))

    result = wsl.run_light_synthesis("issue", iid)

    assert result["action"] == "unparseable"
    assert ws_db.get_extraction(rid) is None
    assert ws_db.get_synthesis("issue", iid) is None


def test_run_light_synthesis_skips_extraction_entry_missing_for_a_raw_item(ws_db, monkeypatch):
    """The LLM's extractions dict need not cover every raw_item id - a
    missing entry just means no extraction gets written for that one,
    never a crash, and the synthesis write still happens."""
    iid = _issue(ws_db)
    _raw_item(ws_db, iid, "k1", "some new content")
    reply = json.loads(_LIGHT_REPLY)  # extractions left empty
    monkeypatch.setattr(wsl, "_run_headless_claude", lambda prompt, timeout: _FakeProc(json.dumps(reply)))

    result = wsl.run_light_synthesis("issue", iid)

    assert result["action"] == "synthesized_light"
    assert result["extracted"] == 0
    assert ws_db.get_synthesis("issue", iid) is not None
