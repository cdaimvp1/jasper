"""Tests for workgraph_status_report.py (Workload Status Update Report
skill). Stage 1 helpers are pure functions over plain dicts/lists - tested
directly, no DB needed. build_stage1_rows itself is a thin real-DB
integration test via the ws_db fixture. Stage 2 never invokes a real
`claude -p` subprocess - _run_headless_claude is monkeypatched, same
discipline as test_workgraph_synthesis_light.py's _FakeProc pattern."""
from __future__ import annotations

import json
import time

import workgraph_status_report as sr


class _FakeProc:
    def __init__(self, stdout, stderr=""):
        self.stdout = stdout
        self.stderr = stderr


# --- Stage 1 pure helpers ----------------------------------------------------

def test_vendor_name_prefers_relationship_over_party_company(monkeypatch):
    monkeypatch.setattr(sr.ws, "list_relationships_for_project",
                         lambda pid: [{"name": "Sodalis"}])
    parties_by_issue = {"i1": [{"affiliation": "external", "company": "Some Other Co"}]}
    assert sr._vendor_name_for_project("proj-1", ["i1"], parties_by_issue) == "Sodalis"


def test_vendor_name_falls_back_to_external_party_companies(monkeypatch):
    monkeypatch.setattr(sr.ws, "list_relationships_for_project", lambda pid: [])
    parties_by_issue = {
        "i1": [{"affiliation": "external", "company": "SAP SE"}],
        "i2": [{"affiliation": "internal", "company": "Eli Lilly"},
               {"affiliation": "external", "company": "SAP SE"}],
    }
    assert sr._vendor_name_for_project("proj-1", ["i1", "i2"], parties_by_issue) == "SAP SE"


def test_vendor_name_none_when_nothing_real_linked(monkeypatch):
    monkeypatch.setattr(sr.ws, "list_relationships_for_project", lambda pid: [])
    assert sr._vendor_name_for_project("proj-1", ["i1"], {"i1": []}) is None


def test_stakeholders_dedupes_and_sorts_by_name():
    parties_by_issue = {
        "i1": [{"id": "p1", "display_name": "Marc Lane"}, {"id": "p2", "display_name": "Gaurav Arora"}],
        "i2": [{"id": "p1", "display_name": "Marc Lane"}],  # same party on a second issue - not double-counted
    }
    result = sr._stakeholders_for_project(["i1", "i2"], parties_by_issue)
    assert result == "Gaurav Arora, Marc Lane"


def test_stakeholders_caps_with_honest_overflow_note():
    parties_by_issue = {"i1": [{"id": f"p{n}", "display_name": f"Person {n:02d}"} for n in range(15)]}
    result = sr._stakeholders_for_project(["i1"], parties_by_issue)
    assert result.endswith(", +3 more")
    assert result.count(",") == sr._STAKEHOLDER_CAP  # CAP names joined by ", " plus the overflow note


def test_stakeholders_none_when_no_parties_linked():
    assert sr._stakeholders_for_project(["i1"], {"i1": []}) is None


def _date_claim(text, *, kind="hard", status="open", ts=0.0, claim_type="date"):
    return {"claim_type": claim_type, "date_kind": kind, "status": status, "text": text, "first_seen_ts": ts}


def test_end_date_claim_none_when_no_hard_date_claim():
    claims_by_issue = {"i1": [_date_claim("shooting for next week", kind="soft")]}
    assert sr._project_end_date_claim(["i1"], claims_by_issue) is None


def test_end_date_claim_ignores_resolved_claims():
    claims_by_issue = {"i1": [_date_claim("Term End Date: May 31, 2031", status="superseded")]}
    assert sr._project_end_date_claim(["i1"], claims_by_issue) is None


def test_end_date_claim_prefers_end_hinted_text_over_incidental_hard_date():
    claims_by_issue = {"i1": [
        _date_claim("OFFER VALID THROUGH: June 26, 2026", ts=2.0),
        _date_claim("Term End Date: May 31, 2031 (contract term end)", ts=1.0),
    ]}
    result = sr._project_end_date_claim(["i1"], claims_by_issue)
    assert "May 31, 2031" in result
    assert result.endswith("(captured deadline claim)")


def test_end_date_claim_picks_most_recent_when_no_hint_matches():
    claims_by_issue = {"i1": [
        _date_claim("by EOD June 30", ts=1.0),
        _date_claim("due by EOD July 4", ts=2.0),
    ]}
    result = sr._project_end_date_claim(["i1"], claims_by_issue)
    assert "July 4" in result


def _money_claim(text, *, status="open", claim_type="decision"):
    return {"claim_type": claim_type, "status": status, "text": text}


def test_spend_claim_extracts_largest_qualifying_amount():
    claims_by_issue = {"i1": [
        _money_claim("PO amount is $50,000"),
        _money_claim("credits of $5,000,000 were applied"),  # a real amount, still a real claim - no cue filtering here, just "largest found"
    ]}
    result = sr._project_spend_claim(["i1"], claims_by_issue)
    assert result == "$5,000,000 (captured financial claim)"


def test_spend_claim_handles_million_suffix():
    claims_by_issue = {"i1": [_money_claim("contract value is $2.5 million")]}
    assert sr._project_spend_claim(["i1"], claims_by_issue) == "$2,500,000 (captured financial claim)"


def test_spend_claim_ignores_dismissed_claims():
    claims_by_issue = {"i1": [_money_claim("$500,000", status="dismissed")]}
    assert sr._project_spend_claim(["i1"], claims_by_issue) is None


def test_spend_claim_ignores_date_claims_even_with_a_dollar_figure():
    claims_by_issue = {"i1": [_money_claim("$500,000", claim_type="date")]}
    assert sr._project_spend_claim(["i1"], claims_by_issue) is None


def test_spend_claim_none_below_floor():
    claims_by_issue = {"i1": [_money_claim("a $50 late fee applies")]}
    assert sr._project_spend_claim(["i1"], claims_by_issue) is None


def test_spend_claim_none_when_no_claims():
    assert sr._project_spend_claim(["i1"], {"i1": []}) is None


# --- build_stage1_rows (real DB, no LLM) ------------------------------------

def test_build_stage1_rows_populates_real_fields_and_leaves_subjective_columns_blank(ws_db):
    pid = ws_db.create_project_with_new_id(name="SAP RISE Cyber Items", category="contract")
    iid = ws_db.create_issue_with_new_id(title="SAP RISE", state="active", category="contract")
    ws_db.assign_issue_to_project(iid, pid, reason="test")

    rid = ws_db.insert_raw_item(
        source="outlook_mail", stable_key="k1", thread_key="k1", dedupe_key="k1",
        occurred_ts=time.time(), subject="s", from_actor="a@sap.com",
        participants_json="[]", body_preview="body",
    )
    ws_db.link_raw_item_to_issue(rid, iid)
    ws_db.insert_claim(
        issue_id=iid, raw_item_id=rid, claim_type="date", text="Term End Date: May 31, 2031",
        author="counterparty", author_basis="direction", date_kind="hard",
    )
    ws_db.insert_claim(
        issue_id=iid, raw_item_id=rid, claim_type="decision", text="PO amount is $1,200,000",
        author="counterparty", author_basis="direction",
    )
    ws_db.upsert_party(id="party-1", primary_email="rep@sap.com", display_name="SAP Rep",
                        affiliation="external", affiliation_confidence="H",
                        affiliation_source="test", company="SAP SE")
    ws_db.link_party_to_issue(iid, "party-1", role="vendor")
    ws_db.upsert_synthesis(
        entity_type="project", entity_id=pid, summary="Real synthesized summary.",
        next_steps_json="[]", suggested_actions_json="[]", synthesized_from_marker="m1",
    )

    rows = sr.build_stage1_rows()
    assert len(rows) == 1
    row = rows[0]
    assert row["project_id"] == pid
    assert row["description"] == "Real synthesized summary."
    assert row["vendor"] == "SAP SE"
    assert row["stakeholders"] == "SAP Rep"
    assert "May 31, 2031" in row["date_cell"]
    assert "(captured deadline claim)" in row["date_cell"]
    assert row["end_date_captured"] is True
    assert row["spend"] == "$1,200,000 (captured financial claim)"
    assert row["resourcing"] == ""
    assert row["complexity"] == ""
    assert row["visibility"] == ""


def _give_real_substance(ws_db, project_id):
    """Attaches one real, currently-open 'ask' claim to a project - the
    minimum needed to survive build_stage1_rows' own _has_real_substance
    gate (2026-08-11, Marc's direct correction: status alone filtered out
    almost nothing, since nearly the whole DB sits at status='active')."""
    iid = ws_db.create_issue_with_new_id(title="Real work", state="active", category="other")
    ws_db.assign_issue_to_project(iid, project_id, reason="test")
    rid = ws_db.insert_raw_item(
        source="outlook_mail", stable_key=f"k-{project_id}", thread_key=f"k-{project_id}",
        dedupe_key=f"k-{project_id}", occurred_ts=time.time(), subject="s",
        from_actor="a@example-vendor.com", participants_json="[]", body_preview="body",
    )
    ws_db.link_raw_item_to_issue(rid, iid)
    ws_db.insert_claim(issue_id=iid, raw_item_id=rid, claim_type="ask",
                        text="Please approve this", author="counterparty", author_basis="direction")
    return iid


def test_build_stage1_rows_excludes_done_and_dismissed_projects(ws_db):
    ws_db.create_project_with_new_id(name="Done project", category="other", status="done")
    ws_db.create_project_with_new_id(name="Dismissed project", category="other", status="dismissed")
    active_pid = ws_db.create_project_with_new_id(name="Active project", category="other")
    _give_real_substance(ws_db, active_pid)

    rows = sr.build_stage1_rows()

    assert [r["project_id"] for r in rows] == [active_pid]


def test_build_stage1_rows_excludes_projects_with_no_real_substance(ws_db):
    """2026-08-11, Marc's direct report: the first real report dumped
    1,525 rows, including literal automated-notification 'projects' with
    zero real claims - status='active' alone is not a noise filter."""
    no_substance_pid = ws_db.create_project_with_new_id(name="Just a notification", category="other")
    ws_db.create_issue_with_new_id(title="No claims here", state="active", category="other")
    substantive_pid = ws_db.create_project_with_new_id(name="Real work", category="other")
    _give_real_substance(ws_db, substantive_pid)

    rows = sr.build_stage1_rows()

    assert [r["project_id"] for r in rows] == [substantive_pid]
    assert no_substance_pid not in [r["project_id"] for r in rows]


# --- Stage 2 (mocked LLM) ----------------------------------------------------

def _stage1_row(project_id="proj-1", end_date_captured=False, date_cell=""):
    return {
        "project_id": project_id, "status": "active", "category": "contract",
        "title": "Some project", "description": "Real description text.",
        "vendor": "", "date_cell": date_cell, "end_date_captured": end_date_captured,
        "stakeholders": "", "resourcing": "", "complexity": "", "visibility": "", "spend": "",
    }


def test_run_stage2_llm_fills_subjective_columns(monkeypatch):
    rows = [_stage1_row()]
    reply = {"judgments": {"proj-1": {
        "resourcing": "Legal SME support needed", "complexity": "High", "visibility": "Medium",
        "end_date_projection": "~Q3 2026",
    }}}
    monkeypatch.setattr(sr, "_run_headless_claude", lambda prompt, timeout, model=None: _FakeProc(json.dumps(reply)))

    result = sr.run_stage2_llm(rows)

    assert result == {"ok": True, "judged": 1, "requested": 1, "batch_failures": []}
    assert rows[0]["resourcing"] == "Legal SME support needed"
    assert rows[0]["complexity"] == "High"
    assert rows[0]["visibility"] == "Medium"
    assert "~Q3 2026" in rows[0]["date_cell"]
    assert "(LLM projection, not a captured date)" in rows[0]["date_cell"]


def test_run_stage2_llm_never_overwrites_a_real_captured_end_date(monkeypatch):
    rows = [_stage1_row(end_date_captured=True, date_cell="May 31, 2031 (captured deadline claim)")]
    reply = {"judgments": {"proj-1": {
        "resourcing": "", "complexity": "Low", "visibility": "Low", "end_date_projection": "~Q4 2026",
    }}}
    monkeypatch.setattr(sr, "_run_headless_claude", lambda prompt, timeout, model=None: _FakeProc(json.dumps(reply)))

    sr.run_stage2_llm(rows)

    assert rows[0]["date_cell"] == "May 31, 2031 (captured deadline claim)"


def test_run_stage2_llm_blank_judgment_leaves_columns_blank(monkeypatch):
    rows = [_stage1_row()]
    reply = {"judgments": {"proj-1": {
        "resourcing": "", "complexity": "", "visibility": "", "end_date_projection": "",
    }}}
    monkeypatch.setattr(sr, "_run_headless_claude", lambda prompt, timeout, model=None: _FakeProc(json.dumps(reply)))

    sr.run_stage2_llm(rows)

    assert rows[0]["resourcing"] == ""
    assert rows[0]["complexity"] == ""
    assert rows[0]["visibility"] == ""
    assert rows[0]["date_cell"] == ""


def test_run_stage2_llm_unparseable_reply_leaves_row_unchanged(monkeypatch):
    rows = [_stage1_row()]
    monkeypatch.setattr(sr, "_run_headless_claude", lambda prompt, timeout, model=None: _FakeProc("not json"))

    result = sr.run_stage2_llm(rows)

    assert result["ok"] is False
    assert result["batch_failures"][0]["reason"] == "unparseable"
    assert rows[0]["complexity"] == ""


def test_run_stage2_llm_timeout_leaves_row_unchanged(monkeypatch):
    import subprocess as sp

    def _raise(*a, **k):
        raise sp.TimeoutExpired(cmd="claude", timeout=1)

    rows = [_stage1_row()]
    monkeypatch.setattr(sr, "_run_headless_claude", _raise)

    result = sr.run_stage2_llm(rows)

    assert result["ok"] is False
    assert result["judged"] == 0
    assert result["requested"] == 1
    assert result["batch_failures"] == [{"start": 0, "reason": "timeout"}]
    assert rows[0]["complexity"] == ""


def test_run_stage2_llm_no_rows_is_a_no_op():
    assert sr.run_stage2_llm([]) == {"ok": True, "judged": 0, "requested": 0, "batch_failures": []}


def test_run_stage2_llm_missing_project_id_in_reply_leaves_that_row_blank(monkeypatch):
    rows = [_stage1_row(project_id="proj-1"), _stage1_row(project_id="proj-2")]
    reply = {"judgments": {"proj-1": {"resourcing": "x", "complexity": "Low", "visibility": "Low",
                                       "end_date_projection": ""}}}
    monkeypatch.setattr(sr, "_run_headless_claude", lambda prompt, timeout, model=None: _FakeProc(json.dumps(reply)))

    result = sr.run_stage2_llm(rows)

    assert result["judged"] == 1
    assert rows[0]["complexity"] == "Low"
    assert rows[1]["complexity"] == ""
