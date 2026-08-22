"""workgraph_entities.py - the canonical Entity layer's identity function and
its read-only audit. Task #379, PHASE 0 ONLY of the plan in
docs/design/CANONICAL_ENTITY_LAYER_DESIGN.md.

=========================================================================
PHASE 0 RAN 2026-08-21. ITS ANSWER IS: DO NOT BUILD PHASES 1-4.
=========================================================================
The design's own section 11 said Phase 0 was worth building even if nothing
else was, because "if the answer turns out to be 'barely any', that is a
legitimate reason not to build the rest." The answer is barely any.

Measured against the live DB, head-to-head against the EXISTING
`workgraph_signals.normalize_company_name`:

    relationships.name   115 values
        normalize_company_name -> 115 keys,   0 collapsing
        entity_key             -> 114 keys,   1 collapsing
        NEW groups entity_key finds:  1  --  ('Fullstory, Inc', 'fullstory')

    parties.company      159 values
        normalize_company_name -> 155 keys,   4 collapsing
        entity_key             -> 155 keys,   4 collapsing
        NEW groups entity_key finds:  0

So the entire measured benefit of the stronger normalizer, across the whole
corpus, is ONE relationship row and 4 project edges. The four collapsing
party-company groups (`ESKO`/`Esko`, `SAP`/`Sap`, `Fullstory`/`Fullstory Inc`,
`Kinaxis`/`Kinaxis Inc.`) are ALREADY collapsed correctly by the existing
normalizer - three are pure case differences and the fourth is a trailing
`Inc.` its regex already handles.

Four new tables, a backfill that DELETES relationship rows, and a read-path
rewrite across four consumers, to fix one row, is not a trade worth making.
This is the same shape as task #388: a reasonable-sounding improvement whose
measured effect did not justify the risk. Recording the number is the
deliverable.

WHAT REMAINS A REAL DEFECT. `Fullstory, Inc` normalizes to `fullstory,` -
with the comma - because `normalize_company_name` strips a trailing legal-form
suffix but never strips punctuation. That is a genuine live miss. Fixing it
belongs in `normalize_company_name`, which feeds the `supplier` data point in
candidate detection and therefore sits under the ROADMAP's standing 2-point
grouping guardrail: it needs an explicit call-out, a regression-corpus
before/after, and a live backtest. It is the design's own open question #4 and
is deliberately NOT bundled here.

WHAT THIS MODULE IS NOW. A read-only diagnostic, wired to nothing. `audit()`
is safe to re-run any time to re-measure whether alias splitting has grown.
`entity_key()` is the measurement instrument that produced the finding above;
it is not used by any decision path and creates no tables.

WHY A SEPARATE MODULE, AND NOT workgraph_signals.py
---------------------------------------------------
`entity_key()` below is a STRONGER normalizer than
`workgraph_signals.normalize_company_name`, and that is exactly why it must
not live next to it. `normalize_company_name` feeds the `supplier` data point
in candidate detection, which sits under the ROADMAP's standing 2-point
grouping guardrail: any change to what counts as a match there needs an
explicit call-out plus a regression-corpus before/after plus a live backtest.

Putting a more aggressive key in the same module as the matching one is how it
gets imported into the matching path by accident six months from now. Physical
separation is the guard. Section 5.1 of the design calls this restraint "the
load-bearing decision here," and section 7 is titled "Does this feed candidate
detection? - No, and that is deliberate."

So: nothing in this file is imported by workgraph_projects.py or
workgraph_pipeline2.py, and nothing here changes any grouping decision.

WHAT entity_key IS FOR
----------------------
Resolving whether two SPELLINGS name the same counterparty, for the Entity
layer only. It is a resolution AID, never an identity - `entities.id` is the
identity. That distinction is the whole point of the layer:
`relationships.normalized_name` is simultaneously the merge key, the identity,
and (via `name`) the label, so improving normalization today means rewriting
identity. Here, improving the key is safe.

WHAT IT DELIBERATELY DOES NOT DO
--------------------------------
No edit-distance, no fuzzy matching, no token-subset matching, no acronym
generation, no LLM call. Token-subset in particular is excluded on purpose:
it would merge `Microsoft` with `Microsoft Ireland Operations` AND `Deloitte`
with `Deloitte Consulting`, and there is no rule that gets the first right
without getting the second wrong. Deterministic and inspectable, matching
`cross_mention_match`'s own stated discipline.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from collections import defaultdict
from typing import Optional

import workgraph_store as ws

# --------------------------------------------------------------- entity_key --

#: Legal-form tokens removed ANYWHERE in the name, repeatedly - not just
#: anchored at the end like normalize_company_name's single trailing suffix.
#: That anchoring is why "Sodalis Inc Ltd" and "Deloitte Consulting LLP"
#: currently keep a legal form in their normalized value.
#: Extended past the current list with the international forms that actually
#: appear in Lilly's supplier base.
_LEGAL_FORM_TOKENS = {
    "inc", "incorporated", "llc", "llp", "lllp", "ltd", "limited",
    "corp", "corporation", "co", "company", "plc", "gmbh", "ag",
    "sa", "sas", "sarl", "bv", "nv", "ab", "as", "oy", "oyj",
    "pty", "kk", "spa", "srl", "aps", "kg", "mbh", "pte",
}

#: Legal forms written with an internal slash. These must be folded BEFORE
#: punctuation stripping, because stripping turns "A/S" into the two tokens
#: "a" and "s", neither of which is in the token set above - so "Novo Nordisk
#: A/S" would keep a dangling "a s". Caught by its own test.
_SLASHED_LEGAL_RE = re.compile(r"\b([as])\s*/\s*([as])\b", re.I)

#: Trailing country/region parenthetical, e.g. "Microsoft Corp (US)".
_TRAILING_PAREN_RE = re.compile(r"\s*\([^()]{1,40}\)\s*$")

#: Everything that is not alphanumeric, whitespace, or an ampersand. `&` is
#: kept because it is load-bearing in real names ("Johnson & Johnson").
_PUNCT_RE = re.compile(r"[^\w\s&]", re.UNICODE)

_WS_RE = re.compile(r"\s+")


def entity_key(name: Optional[str]) -> str:
    """Deterministic resolution key for a company spelling. '' for anything
    degenerate - never a guess, never a fabricated name.

    Steps, in order (design section 5.2):
      1. NFKC-normalize, lowercase
      2. strip punctuation except '&'  -> fixes the comma class, "Fullstory, Inc"
      3. drop a leading "the"
      4. remove legal-form tokens ANYWHERE, repeatedly
      5. drop a trailing country/region parenthetical
      6. collapse whitespace

    Step 5 runs before punctuation stripping would destroy the parentheses,
    so it is applied to the raw-ish string first; the ordering in the docstring
    above is the design's conceptual order, the code below is the working one.
    """
    if not name:
        return ""
    s = unicodedata.normalize("NFKC", str(name)).strip()
    if not s:
        return ""
    # 5 first (needs the parentheses to still exist)
    s = _TRAILING_PAREN_RE.sub("", s)
    s = s.lower()
    # Fold slashed legal forms BEFORE punctuation stripping - see the regex's
    # own comment for why order matters here.
    # A lambda, not a "\1\2" backreference STRING: an earlier revision of
    # this line ended up holding the literal control bytes \x01\x02 rather
    # than the escape sequences, injecting control characters into every
    # folded key. The tests still passed, because the punctuation strip on
    # the next line happens to remove control characters too - a silent
    # wrong-for-the-right-reason pass. A lambda cannot fail that way.
    s = _SLASHED_LEGAL_RE.sub(lambda m: m.group(1) + m.group(2), s)
    # 2
    s = _PUNCT_RE.sub(" ", s)
    s = _WS_RE.sub(" ", s).strip()
    if not s:
        return ""
    tokens = s.split(" ")
    # 3
    if tokens and tokens[0] == "the":
        tokens = tokens[1:]
    # 4
    tokens = [t for t in tokens if t and t not in _LEGAL_FORM_TOKENS]
    return " ".join(tokens).strip()


# ------------------------------------------------------- Phase 0: the audit --

#: Never attach these as a company identifier - network.lilly.com is
#: confirmed-issued to suppliers and contingent workers, so treating it as a
#: company domain would cross-link every supplier onto one entity. Mirrors
#: workgraph_parties.DOMAIN_OVERRIDES' own reasoning.
_NEVER_A_COMPANY_DOMAIN = {"lilly.com", "network.lilly.com", "lists.lilly.com"}


def audit(*, verbose: bool = False) -> dict:
    """PHASE 0 - read-only. Zero writes, no DDL, no LLM. Reports exactly what
    a Phase 2 backfill WOULD do, so the blast radius is a finite reviewed list
    instead of a trusted algorithm.

    Returns counts plus, critically, `would_merge`: the explicit merge list.
    """
    c = ws._connect()

    rels = [dict(r) for r in c.execute(
        "SELECT id, name, normalized_name, status, created_ts "
        "FROM relationships ORDER BY created_ts, id")]

    # --- what Phase 2 would do to `relationships` -------------------------
    by_key: dict[str, list[dict]] = defaultdict(list)
    blank_key = []
    for r in rels:
        k = entity_key(r["name"])
        r["_entity_key"] = k
        (by_key[k] if k else blank_key).append(r)

    would_merge = []
    for k, group in sorted(by_key.items()):
        if len(group) < 2:
            continue
        # created_ts order was applied in the query, so group[0] is oldest =
        # the survivor, matching #343's own tie-break.
        survivor, absorbed = group[0], group[1:]
        # How many project edges actually move? This is the real blast radius.
        moved = 0
        for a in absorbed:
            moved += c.execute(
                "SELECT COUNT(*) FROM project_relationships WHERE relationship_id = ?",
                (a["id"],)).fetchone()[0]
        would_merge.append({
            "entity_key": k,
            "survivor": {"id": survivor["id"], "name": survivor["name"]},
            "absorbed": [{"id": a["id"], "name": a["name"]} for a in absorbed],
            "project_edges_reassigned": moved,
            # Would today's weaker key already have merged these? If yes this
            # is not a new merge at all; if no, entity_key is what found it.
            "already_merged_by_normalized_name":
                len({(r["normalized_name"] or "") for r in group}) == 1,
        })

    # --- the same question for raw party companies (the dashboard's split) --
    party_rows = [dict(r) for r in c.execute(
        "SELECT company, COUNT(*) n FROM parties "
        "WHERE company IS NOT NULL AND TRIM(company) != '' "
        "AND affiliation != 'internal' GROUP BY company")]
    party_keys: dict[str, list[str]] = defaultdict(list)
    for p in party_rows:
        k = entity_key(p["company"])
        if k:
            party_keys[k].append(p["company"])
    party_collapses = {k: v for k, v in party_keys.items() if len(set(v)) > 1}

    # --- email-domain identifier reach, with the exclusion guard applied ---
    dom_rows = [dict(r) for r in c.execute(
        "SELECT company, primary_email FROM parties "
        "WHERE affiliation != 'internal' AND primary_email LIKE '%@%' "
        "AND company IS NOT NULL AND TRIM(company) != ''")]
    domains_by_key: dict[str, set] = defaultdict(set)
    for d in dom_rows:
        dom = d["primary_email"].rsplit("@", 1)[-1].strip().lower()
        if not dom or dom in _NEVER_A_COMPANY_DOMAIN:
            continue
        k = entity_key(d["company"])
        if k:
            domains_by_key[k].add(dom)
    # A domain held by >1 entity_key is a Layer-2 PROPOSAL, never a merge.
    key_by_domain: dict[str, set] = defaultdict(set)
    for k, doms in domains_by_key.items():
        for dom in doms:
            key_by_domain[dom].add(k)
    shared_domain_proposals = {d: sorted(ks) for d, ks in key_by_domain.items()
                               if len(ks) > 1}

    out = {
        "phase": "0 (read-only audit)",
        "wrote_anything": False,
        "relationships": {
            "total": len(rels),
            "distinct_normalized_name": len({(r["normalized_name"] or "") for r in rels}),
            "distinct_entity_key": len(by_key),
            "blank_entity_key": len(blank_key),
        },
        "would_merge_groups": len(would_merge),
        "would_merge_rows_deleted": sum(len(g["absorbed"]) for g in would_merge),
        "would_merge_project_edges_reassigned":
            sum(g["project_edges_reassigned"] for g in would_merge),
        "merges_entity_key_finds_that_normalized_name_missed":
            sum(1 for g in would_merge if not g["already_merged_by_normalized_name"]),
        "would_merge": would_merge,
        "party_companies": {
            "distinct_raw_values": len(party_rows),
            "distinct_entity_key": len(party_keys),
            "keys_with_multiple_spellings": len(party_collapses),
            "examples": dict(sorted(party_collapses.items())[:12]),
        },
        "email_domain_identifiers": {
            "entity_keys_with_at_least_one_domain": len(domains_by_key),
            "shared_domain_proposals": len(shared_domain_proposals),
            "shared_domain_examples": dict(sorted(shared_domain_proposals.items())[:8]),
        },
    }
    if not verbose:
        out.pop("would_merge", None)
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Task #379 Phase 0 - read-only entity audit")
    ap.add_argument("--verbose", action="store_true",
                    help="include the full explicit merge list")
    a = ap.parse_args()
    print(json.dumps(audit(verbose=a.verbose), indent=2, default=str))
