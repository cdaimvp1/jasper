"""
workgraph_status_report.py - "Workload Status Update Report" skill.

Regenerates Marc's manually-maintained status spreadsheet (Lane_Status_
April_2026.xlsx, Sheet1, 9 columns) from Jasper's own graph, in the same
two stages Marc asked for:

Stage 1 (build_stage1_rows): pure, zero-LLM, deterministic. Every column
comes ONLY from real graph data - project title/synthesis summary, real
external parties (issue_parties/parties), a real durable Relationship
name if one is linked (project_relationships/relationships), the
project's real opened_at, and a real captured claim for end date (claims
where claim_type='date', date_kind='hard') / spend (a real dollar figure
found inside a real ask/decision/commitment claim's own text - claim-
grounded, not a raw scan of every email body the way workgraph_nba's
value-at-risk heuristic works). Resourcing Support Needed/Level of
Complexity/Visibility are left blank here on purpose - nothing in the
graph derives them; that is Stage 2's job. Never fabricates: an unfound
field is "", never a guess.

Stage 2 (run_stage2_llm): exactly ONE headless-claude call over the WHOLE
stage-1 table at once (never one call per project - this portfolio can
run into the thousands of active work_objects, and this codebase's own
established per-entity light-synthesis pattern, workgraph_synthesis_
light.run_light_synthesis, is deliberately NOT reused here for that
reason). Fills the three subjective judgment columns, plus an end-date
PROJECTION only on rows Stage 1 left blank - clearly labeled "(LLM
projection, not a captured date)" in the cell text itself, per Marc's own
requirement that a real date never be visually indistinguishable from a
guess. If the call times out or the reply doesn't parse, those columns
just stay blank - never a silent guess, and Stage 1's output is still
written either way.

Subprocess contract (prompt over stdin, explicit utf-8/errors="replace")
copied from workgraph_synthesis_light._run_headless_claude's own real,
hard-won fixes (Windows' ~32K argv ceiling -> WinError 206; Windows' cp1252
stdin default -> UnicodeEncodeError) - same reasoning as that module's own
docstring for why this is a fresh copy, not a shared import: no future
change to one path should silently affect another's timeout/tree-kill
behavior.
"""
from __future__ import annotations

import datetime
import json
import os
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import Optional

import openpyxl

import workgraph_store as ws

_OPEN_PROJECT_STATES = ("active", "waiting", "blocked")

# Verbatim header cells from Lane_Status_April_2026.xlsx, Sheet1, row 1 -
# read directly from the real file rather than retyped, so column
# structure/wording matches exactly (including the two headers' own
# leading spaces).
HEADERS = [
    "Project Name – The official name or working title of the project",
    "Project Description – A brief overview of the project's scope and objectives",
    "Vendor Name – The name of any external vendor(s) involved, if applicable",
    " Project Start & End Dates – Confirmed or estimated timelines",
    "Stakeholder Name(s) – Key internal and/or external stakeholders associated with the project",
    "Resourcing Support Needed – Please indicate if you are in need of additional resources or "
    "support on any project, and briefly describe the type of assistance that would be most helpful",
    "Level of Complexity – Please rate each project as Low, Medium, or High complexity",
    "Visibility: High, Medium, Low – Where this sits from a leadership perspective "
    "(both the stakeholders’ and ours)",
    " Anticipated Spend – Estimated budget or projected expenditure for the project",
]


# --- Stage 1: deterministic, real-data-only helpers ------------------------

def _format_ts(ts: Optional[float]) -> str:
    if not ts:
        return ""
    dt = datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc)
    return f"{dt:%b} {dt.day}, {dt.year}"


def _vendor_name_for_project(project_id: str, issue_ids: list[str],
                              parties_by_issue: dict[str, list[dict]]) -> Optional[str]:
    """Prefers a real durable Relationship name (project_relationships/
    relationships - the one deliberately-named "this IS the vendor
    relationship" entity, task #304) over a raw external-party company
    rollup, since a Relationship is the more authoritative name when one
    exists. Falls back to the distinct external-party companies actually
    linked to this project's own issues (issue_parties/parties), most-
    common first, capped so a project spanning many contacts at the same
    handful of companies doesn't produce an unreadable cell. None (never a
    guess) if nothing real is linked either way."""
    relationships = ws.list_relationships_for_project(project_id)
    if relationships:
        return "; ".join(sorted({r["name"] for r in relationships}))

    companies: Counter = Counter()
    for issue_id in issue_ids:
        for party in parties_by_issue.get(issue_id, []):
            if party.get("affiliation") == "external" and party.get("company"):
                companies[party["company"]] += 1
    if not companies:
        return None
    top = [company for company, _ in companies.most_common(5)]
    return "; ".join(top)


_STAKEHOLDER_CAP = 12


def _stakeholders_for_project(issue_ids: list[str],
                               parties_by_issue: dict[str, list[dict]]) -> Optional[str]:
    """Real internal + external parties actually linked to this project's
    own issues (issue_parties), deduped by party id, name-sorted. Capped
    at _STAKEHOLDER_CAP with an honest "+N more" rather than silently
    truncating - never a guess when the graph has zero linked parties."""
    seen: dict[str, str] = {}
    for issue_id in issue_ids:
        for party in parties_by_issue.get(issue_id, []):
            name = party.get("display_name") or party.get("primary_email")
            if name and party.get("id"):
                seen[party["id"]] = name
    names = sorted(seen.values())
    if not names:
        return None
    if len(names) > _STAKEHOLDER_CAP:
        return ", ".join(names[:_STAKEHOLDER_CAP]) + f", +{len(names) - _STAKEHOLDER_CAP} more"
    return ", ".join(names)


_END_DATE_HINT_RE = re.compile(
    r"\b(end date|expir|term end|non-renewal|termination|contract term)\b", re.I)
_MAX_CLAIM_CELL_CHARS = 140


def _project_end_date_claim(issue_ids: list[str], claims_by_issue: dict[str, list[dict]]) -> Optional[str]:
    """A real captured deadline claim (claims.claim_type='date',
    date_kind='hard', status='open') tied to one of this project's own
    issues - never a resolved/superseded one, never a soft/aspirational
    one. Among several, prefers text that reads like an actual end/
    expiration/renewal date over an incidental hard date (e.g. an "offer
    valid through" cutoff on the same thread); among ties, the most
    recently captured. Returns the claim's own text with a provenance
    label baked into the cell text itself, per Marc's requirement that
    this never look like an unlabeled real date - or None if no real hard
    date claim exists (never guessed)."""
    hard_dates = [
        c for issue_id in issue_ids
        for c in claims_by_issue.get(issue_id, [])
        if c.get("claim_type") == "date" and c.get("date_kind") == "hard" and c.get("status") == "open"
    ]
    if not hard_dates:
        return None
    hinted = [c for c in hard_dates if _END_DATE_HINT_RE.search(c.get("text") or "")]
    pool = hinted or hard_dates
    pool.sort(key=lambda c: c.get("first_seen_ts") or 0, reverse=True)
    text = (pool[0].get("text") or "").strip()
    if len(text) > _MAX_CLAIM_CELL_CHARS:
        text = text[:_MAX_CLAIM_CELL_CHARS - 3] + "..."
    return f"{text} (captured deadline claim)"


_DOLLAR_RE = re.compile(
    r"\$\s?([\d,]+(?:\.\d+)?)\s*(million|mm|billion|bn|thousand|k|m|b)?\b", re.I)
_DOLLAR_SUFFIX_MULTIPLIER = {
    "million": 1_000_000, "mm": 1_000_000, "m": 1_000_000,
    "billion": 1_000_000_000, "bn": 1_000_000_000, "b": 1_000_000_000,
    "thousand": 1_000, "k": 1_000,
}
_SPEND_FLOOR = 1_000.0  # below this, not worth presenting as "anticipated spend" - matches workgraph_nba._VALUE_FLOOR's own reasoning
_SPEND_CLAIM_TYPES = ("decision", "commitment", "ask")


def _extract_dollar_amounts(text: str) -> list[float]:
    amounts = []
    for match in _DOLLAR_RE.finditer(text or ""):
        try:
            value = float(match.group(1).replace(",", ""))
        except ValueError:
            continue
        suffix = (match.group(2) or "").lower()
        amounts.append(value * _DOLLAR_SUFFIX_MULTIPLIER.get(suffix, 1))
    return amounts


def _project_spend_claim(issue_ids: list[str], claims_by_issue: dict[str, list[dict]]) -> Optional[str]:
    """A real dollar figure found inside a real, currently-open ask/
    decision/commitment claim's own text for one of this project's
    issues - claim-grounded (curator already judged this text worth
    materializing as a claim), not a raw scan of every linked email body
    the way workgraph_nba._extract_value_amount works for the Value-at-
    Risk banner. Takes the largest qualifying figure found; None (never a
    guess, never $0) if no open claim on this project mentions a real
    dollar amount."""
    best: Optional[tuple[float, str]] = None
    for issue_id in issue_ids:
        for claim in claims_by_issue.get(issue_id, []):
            if claim.get("status") != "open" or claim.get("claim_type") not in _SPEND_CLAIM_TYPES:
                continue
            for amount in _extract_dollar_amounts(claim.get("text") or ""):
                if amount < _SPEND_FLOOR:
                    continue
                if best is None or amount > best[0]:
                    best = (amount, claim["text"])
    if best is None:
        return None
    amount, _text = best
    return f"${amount:,.0f} (captured financial claim)"


_SUBSTANCE_CLAIM_TYPES = ("ask", "decision", "commitment")


def _has_real_substance(claims_by_member: dict[str, list[dict]]) -> bool:
    """True only if at least one currently-open ask/decision/commitment
    claim exists anywhere under this project's real members (2026-08-11,
    Marc's own direct correction after reviewing the first real output -
    it dumped 1,525 rows, nearly every project in the DB, including
    several literal automated Yammer/Viva Engage group-notification
    "projects" with zero real business content). A 'date' claim alone
    does NOT count - a calendar reminder with no real ask/decision/
    commitment attached is exactly the kind of row Marc flagged."""
    for claims in claims_by_member.values():
        for claim in claims:
            if claim.get("status") == "open" and claim.get("claim_type") in _SUBSTANCE_CLAIM_TYPES:
                return True
    return False


def _rollup_relationship_row(relationship_name: str, members: list[dict]) -> dict:
    """One row per durable Relationship (task #304) that has 2+ real,
    substantive projects linked to it (2026-08-11, Marc's own explicit
    request: "roll up by vendor, not by project, so SAP shows as one
    line, not four"). Aggregates real per-project data only - never
    invents a combined figure; multiple real captured spend/date claims
    are listed together rather than summed (a sum across unrelated
    workstreams under the same vendor would misrepresent each one)."""
    titles = [m["title"] for m in members]
    descriptions = [f"{m['title']}: {m['description']}" if m["description"] else m["title"] for m in members]
    stakeholders: dict[str, str] = {}
    for m in members:
        for name in (m["stakeholders"] or "").split(", "):
            name = name.strip()
            if name and not name.startswith("+"):
                stakeholders[name.lower()] = name
    starts = sorted(m["opened_at"] for m in members if m.get("opened_at"))
    date_cells = [m["date_cell"] for m in members if m["date_cell"]]
    spends = [m["spend"] for m in members if m["spend"]]
    categories = Counter(m["category"] for m in members if m["category"])

    return {
        "project_id": f"relationship:{relationship_name}",
        "status": "active",
        "category": categories.most_common(1)[0][0] if categories else "",
        "title": relationship_name,
        "description": " | ".join(descriptions),
        "vendor": relationship_name,
        "date_cell": "; ".join(date_cells) if date_cells else _format_ts(starts[0]) if starts else "",
        "opened_at": starts[0] if starts else None,
        "end_date_captured": any(m["end_date_captured"] for m in members),
        "stakeholders": ", ".join(sorted(stakeholders.values())[:_STAKEHOLDER_CAP]) or "",
        "resourcing": "",
        "complexity": "",
        "visibility": "",
        "spend": "; ".join(spends) if spends else "",
        "_rolled_up_titles": titles,
    }


def build_stage1_rows() -> list[dict]:
    """One row per real, substantive project - or, when 2+ substantive
    projects share a durable Relationship (task #304), one rolled-up row
    per Relationship instead (Marc's own explicit request, 2026-08-11).
    Status stays limited to active/waiting/blocked (never done/dismissed/
    archived/noise-archived), and on top of that a project must clear
    _has_real_substance - status alone filtered out almost nothing
    (1,525 of 1,527 projects in the DB are "active"), so it is not a
    real noise filter by itself. Members are gathered the same way
    workgraph_pipeline2.run_project_extraction does - clusters AND real
    issues - not issues alone, since many real claims still sit on an
    unpromoted cluster. Every field either comes from real graph data or
    is left "" - Stage 2 is the only place a subjective judgment or a
    projected date gets added, and only additively (see run_stage2_llm).

    Coverage caveat, honest and worth knowing before reading row counts:
    as of this build, only ~27 of 1,525 active projects have ANY claim
    materialized yet under their real members - task #310's stale-marker
    backfill (still running as of this fix) is the actual remediation for
    that gap, not this report. This report will under-count real active
    work until that backfill (or the live pipeline going forward) catches
    up - re-run it periodically rather than trusting one snapshot."""
    substantive_rows: dict[str, dict] = {}
    relationship_members: dict[str, list[dict]] = {}
    relationship_names: dict[str, str] = {}

    for project in ws.list_projects(status=list(_OPEN_PROJECT_STATES)):
        project_id = project["id"]
        member_ids = (
            [c["id"] for c in ws.list_clusters_for_project(project_id)]
            + [i["id"] for i in ws.list_issues_for_project(project_id)]
        )
        parties_by_member = ws.list_parties_for_issues(member_ids) if member_ids else {}
        claims_by_member = ws.list_claims_for_issues(member_ids) if member_ids else {}

        if not _has_real_substance(claims_by_member):
            continue

        synthesis = ws.get_synthesis("project", project_id)
        start_label = _format_ts(project.get("opened_at"))
        end_claim = _project_end_date_claim(member_ids, claims_by_member)
        date_cell = f"{start_label} – {end_claim}" if (start_label and end_claim) else (end_claim or start_label)

        row = {
            "project_id": project_id,
            "status": project.get("status"),
            "category": project.get("category") or "",
            "title": project.get("display_title") or project.get("name") or project_id,
            "description": (synthesis or {}).get("summary") or "",
            "vendor": _vendor_name_for_project(project_id, member_ids, parties_by_member) or "",
            "date_cell": date_cell,
            "opened_at": project.get("opened_at"),
            "end_date_captured": bool(end_claim),
            "stakeholders": _stakeholders_for_project(member_ids, parties_by_member) or "",
            "resourcing": "",
            "complexity": "",
            "visibility": "",
            "spend": _project_spend_claim(member_ids, claims_by_member) or "",
        }
        substantive_rows[project_id] = row

        for relationship in ws.list_relationships_for_project(project_id):
            relationship_members.setdefault(relationship["id"], []).append(row)
            relationship_names[relationship["id"]] = relationship["name"]

    rolled_up_project_ids: set[str] = set()
    final_rows: list[dict] = []
    for relationship_id, members in relationship_members.items():
        if len(members) < 2:
            continue  # a lone project under a relationship reads better standalone than as a 1-member "rollup"
        final_rows.append(_rollup_relationship_row(relationship_names[relationship_id], members))
        rolled_up_project_ids.update(m["project_id"] for m in members)

    for project_id, row in substantive_rows.items():
        if project_id not in rolled_up_project_ids:
            final_rows.append(row)
    return final_rows


# --- Stage 2: exactly one LLM pass over the whole stage-1 table ------------

_STAGE2_TIMEOUT_SECONDS = 1200  # generous on purpose - one real pass over a whole portfolio at once, run on demand, never on a periodic tick
_MAX_DESCRIPTION_CHARS = 320  # per-project cap so the aggregate prompt stays one bounded payload even across a large portfolio

_STAGE2_PROMPT_HEADER = """You are helping fill in three subjective judgment-call columns on a real internal status-report spreadsheet, for a portfolio of real active vendor/procurement projects tracked by an internal tool. For EACH project below, given only its real title/category/description, make your best-effort, honest judgment call - a subjective rating IS the point (a human reviews and corrects every one of these by hand afterward), but never invent a FACT that isn't implied by the text.

For EACH project return:
  - resourcing: a short (under 15 words) note on what additional resourcing/support this project plausibly needs, or "" if the description gives no real basis to say anything.
  - complexity: one of "Low", "Medium", "High" - or "" if there is truly no real project content to judge from.
  - visibility: one of "Low", "Medium", "High" (leadership-visibility) - or "" if there is truly no real project content to judge from.
  - end_date_projection: ONLY meaningful when "needs_end_date_projection" is true for that project - your best-effort FORECAST of when this might wrap up, as a short label like "~Q3 2026" or "~late 2026" (never a specific day-level date - you have no real basis for that precision). Return "" when needs_end_date_projection is false, or when there is truly no basis to guess.

Be honest when a project is clearly NOT real substantive work (an out-of-office autoreply, a forwarded personal email with no business content, an empty message, a phishing banner, etc.) - for those, return "" for every field rather than inventing a plausible-sounding but fabricated judgment.

Projects (JSON array):
{projects_json}

Output EXACTLY one JSON object, nothing before or after it, in this shape:
{{"judgments": {{"<project_id>": {{"resourcing": "...", "complexity": "...", "visibility": "...", "end_date_projection": "..."}}, ...}}}}
Every project_id from the input array above must appear as a key in "judgments", even if every value for it is "".
"""


def _run_headless_claude(prompt: str, *, timeout: int, model: Optional[str] = None) -> subprocess.CompletedProcess:
    """Same real, hard-won subprocess contract as workgraph_synthesis_
    light._run_headless_claude (a fresh copy, not a shared import - see
    that function's own docstring for why): prompt over stdin, never argv
    (Windows' ~32K total-command-line ceiling -> WinError 206 on a prompt
    this size), explicit encoding="utf-8", errors="replace" (Windows
    otherwise falls back to the cp1252 locale codepage for the stdin
    write, which crashes on real project text)."""
    env = os.environ.copy()
    args = ["claude", "-p", "--allowedTools", ""]
    if model:
        args += ["--model", model]
    proc = subprocess.Popen(
        args,
        cwd=str(Path(__file__).resolve().parent), env=env,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace",
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
    )
    try:
        stdout, stderr = proc.communicate(input=prompt, timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True, timeout=15)
        except Exception:
            pass
        proc.communicate()
        raise
    return subprocess.CompletedProcess(proc.args, proc.returncode, stdout, stderr)


def _parse_stage2_output(stdout: str) -> Optional[dict]:
    """Same lenient outermost-braces extraction this codebase already uses
    for every other headless-subprocess reply (_parse_light_output,
    _parse_verdict, etc.) - a real `claude -p` reply can carry stray prose
    around the JSON despite the prompt's instruction not to."""
    text = stdout or ""
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        parsed = json.loads(text[start:end + 1])
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(parsed, dict):
        return None
    judgments = parsed.get("judgments")
    return judgments if isinstance(judgments, dict) else None


_STAGE2_BATCH_SIZE = 50  # 2026-08-11 (report-quality fix): the original design made ONE call for
# the whole table, which is exactly why every enrichment column came back blank in the first real
# run - a single-shot reply covering hundreds of projects either times out or gets truncated before
# valid closing JSON, and _parse_stage2_output then discards the WHOLE reply, not just the broken
# part. Chunking means one bad/truncated batch only blanks its own ~50 rows, not the entire report.


def _apply_stage2_judgments(rows: list[dict], judgments: dict) -> int:
    judged = 0
    for row in rows:
        entry = judgments.get(row["project_id"])
        if not isinstance(entry, dict):
            continue
        resourcing = entry.get("resourcing")
        if isinstance(resourcing, str) and resourcing.strip():
            row["resourcing"] = resourcing.strip()
        complexity = entry.get("complexity")
        if complexity in ("Low", "Medium", "High"):
            row["complexity"] = complexity
        visibility = entry.get("visibility")
        if visibility in ("Low", "Medium", "High"):
            row["visibility"] = visibility
        if not row["end_date_captured"]:
            projection = entry.get("end_date_projection")
            if isinstance(projection, str) and projection.strip():
                label = f"{projection.strip()} (LLM projection, not a captured date)"
                row["date_cell"] = f"{row['date_cell']} – {label}" if row["date_cell"] else label
        judged += 1
    return judged


def run_stage2_llm(rows: list[dict], *, model: Optional[str] = None,
                    timeout: int = _STAGE2_TIMEOUT_SECONDS,
                    batch_size: int = _STAGE2_BATCH_SIZE) -> dict:
    """Mutates `rows` in place: fills resourcing/complexity/visibility on
    every row, and appends a clearly-labeled end-date projection only onto
    rows Stage 1 left with no captured end date. Runs in chunks of
    batch_size (see _STAGE2_BATCH_SIZE) rather than one call for the whole
    table, so a single truncated/unparseable reply only blanks its own
    batch. Never crashes or fabricates on failure - a batch's timeout or
    unparseable reply just leaves that batch's subjective columns "" and
    moves on to the next batch; the return value reports per-batch
    failures honestly rather than silently swallowing them."""
    if not rows:
        return {"ok": True, "judged": 0, "requested": 0, "batch_failures": []}

    judged_total = 0
    batch_failures = []
    for start in range(0, len(rows), batch_size):
        batch = rows[start:start + batch_size]
        payload = [{
            "id": row["project_id"],
            "title": (row["title"] or "")[:160],
            "category": row.get("category") or "",
            "description": (row["description"] or "")[:_MAX_DESCRIPTION_CHARS],
            "needs_end_date_projection": not row["end_date_captured"],
        } for row in batch]
        prompt = _STAGE2_PROMPT_HEADER.format(projects_json=json.dumps(payload, ensure_ascii=False))

        try:
            proc = _run_headless_claude(prompt, timeout=timeout, model=model)
        except subprocess.TimeoutExpired:
            batch_failures.append({"start": start, "reason": "timeout"})
            continue

        judgments = _parse_stage2_output(proc.stdout)
        if judgments is None:
            batch_failures.append({"start": start, "reason": "unparseable",
                                    "stderr": (proc.stderr or "")[:500]})
            continue

        judged_total += _apply_stage2_judgments(batch, judgments)

    return {"ok": not batch_failures, "judged": judged_total, "requested": len(rows),
            "batch_failures": batch_failures}


# --- Output ------------------------------------------------------------------

def write_xlsx(rows: list[dict], output_path: str) -> None:
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "Sheet1"
    sheet.append(HEADERS)
    for row in rows:
        sheet.append([
            row["title"], row["description"], row["vendor"], row["date_cell"],
            row["stakeholders"], row["resourcing"], row["complexity"], row["visibility"], row["spend"],
        ])
    workbook.save(output_path)


def generate_report(output_path: str, *, skip_llm: bool = False, model: Optional[str] = None) -> dict:
    """The full two-stage pipeline: Stage 1 always runs (real data only);
    Stage 2 runs unless skip_llm - and either way, write_xlsx always runs
    last, so a Stage 2 failure/timeout still leaves a real, real-data-only
    Stage 1 spreadsheet on disk rather than nothing at all."""
    rows = build_stage1_rows()
    stage2 = {"ok": True, "skipped": True} if skip_llm else run_stage2_llm(rows, model=model)
    write_xlsx(rows, output_path)
    return {"output_path": output_path, "project_count": len(rows), "stage2": stage2}


if __name__ == "__main__":
    out_path = sys.argv[1] if len(sys.argv) > 1 else str(Path.home() / "Downloads" / "Workload_Status_Update_Report.xlsx")
    print(json.dumps(generate_report(out_path), indent=2))
