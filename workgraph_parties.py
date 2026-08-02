"""
workgraph_parties.py — deterministic party (contact) extraction + affiliation
classification. No LLM calls, no directory lookup (confirmed none exists in
this toolset - the only Graph identity tool available is get_me, which
returns the signed-in user's own profile, not an arbitrary-user lookup).

Affiliation is a best-effort domain heuristic, explicitly NOT authoritative:
a lilly.com address does not reliably mean "Lilly employee" - some suppliers
are provisioned on Lilly's network with lilly.com-style guest/vendor
addresses. Every party's affiliation carries a confidence tier and a source
field so the UI can surface uncertain ones for Marc to correct, and a
correction (workgraph_store.correct_party_affiliation) sticks permanently -
this module never re-guesses a party once corrected.

Identity resolution limitation, stated plainly rather than papered over:
raw_items.from_actor/participants are a MIX of email addresses and bare
display names across sources (Outlook's To/CC properties return display
names unless already SMTP-resolved; Teams falls back to displayName when a
member has no email in the payload). A bare display name can't be turned
into a NEW identity without guessing (no directory lookup exists), so this
module NEVER fabricates a Party from a name alone.

It DOES try to resolve a bare name against a party that already exists,
two ways, cheapest/most-certain first (_resolve_bare_name):
  1. Exact match against a party's own `display_name`, once one is known.
  2. A deterministic corporate-email-convention guess from the name itself
     (firstlast / first.last / flast / etc.) checked against every known
     party's email LOCAL PART - e.g. "Ashlie Jonte" -> candidate "ajonte"
     matches an existing party at ajonte@lilly.com. Only string transforms,
     no fuzzy/similarity matching, no LLM. If more than one existing party
     matches (a real collision risk with common names), it abstains - same
     discipline as everything else here, a wrong resolution that STICKS is
     worse than an honest miss.
A name that resolves this way is linked to the EXISTING party (never a new
one) and, via upsert_party's own "fill in display_name only if not already
set" behavior, teaches that party's display_name for next time - so a name
seen once via the slower local-part guess resolves instantly via exact
match on every later occurrence. A name that resolves neither way is
counted in `skipped_name_only`, same honest-abstain behavior as before.
"""
from __future__ import annotations

import re
import sys
import time
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
import workgraph_store as ws
import workgraph_signals

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# workgraph_signals._SYSTEM_SENDER (canonical definition, was duplicated across 4
# modules) - a no-reply/system sender's domain-derived "company" isn't a real
# supplier name (e.g. 'Ansmtp' from no-reply@ansmtp.ariba.com was showing up as a
# top "external company" with 57 issues before this fix - found during the
# 2026-07-29 backlog profiling pass). Applied here so the bogus name never gets
# INTO the parties table in the first place, not just skipped later by title
# generation.

# _MACHINE_SIGNAL_DOMAINS/_is_machine_signal_domain moved to workgraph_signals.py
# (2026-08-02, task #53), folded into workgraph_signals.is_automated_sender()
# used below - workgraph_projects.py's grouping code needed the same
# combined check this module already did (see that function's docstring for
# why: a real live over-merge, proj-012 "Adobesign," 15 unrelated issues
# wrongly combined, because the grouping code only ever checked
# _SYSTEM_SENDER, never this domain list).

INTERNAL_DOMAIN = "lilly.com"

# Distribution-list infrastructure, not a person - found in real data during
# the initial backfill (procurement_us@, finance_all_employees@, etc., all
# under this subdomain). Excluded entirely rather than mis-filed as a Party.
NON_PERSON_DOMAINS = {"lists.lilly.com"}

# Confirmed exceptions to the plain lilly.com-domain heuristic - a lilly.com
# SUBDOMAIN that looks internal but isn't reliably so. network.lilly.com:
# confirmed by Marc (2026-07-28) as issued to contingent workers and
# suppliers - "sometimes/sometimes", so never assumed a single company name,
# but the affiliation itself is now a confirmed pattern, not a guess, hence
# H confidence / 'confirmed_exception' source rather than the generic
# domain_heuristic's M/'domain_heuristic'.
DOMAIN_OVERRIDES = {
    "network.lilly.com": {"affiliation": "external", "affiliation_confidence": "H",
                           "affiliation_source": "confirmed_exception", "company": None},
}


def _looks_like_email(s: str) -> bool:
    if not s or not _EMAIL_RE.match(s.strip()):
        return False
    domain = s.strip().lower().rsplit("@", 1)[-1]
    return domain not in NON_PERSON_DOMAINS


def _company_from_domain(domain: str) -> str:
    """A readable guess only - e.g. 'acmecorp.com' -> 'Acmecorp'. Not
    authoritative, just better than a bare domain string in the UI."""
    label = domain.split(".")[0]
    return label.replace("-", " ").title()


def classify_affiliation(email: str) -> dict:
    """Domain heuristic, checked in order: a confirmed override first, then
    the plain lilly.com check (M confidence - the guest-account exception
    means this ISN'T certain), then anything else -> external at H
    confidence (a genuinely different domain is unambiguous)."""
    domain = email.rsplit("@", 1)[-1].lower()
    if domain in DOMAIN_OVERRIDES:
        return dict(DOMAIN_OVERRIDES[domain])
    if domain == INTERNAL_DOMAIN:
        return {"affiliation": "internal", "affiliation_confidence": "M",
                "affiliation_source": "domain_heuristic", "company": None}
    if workgraph_signals.is_automated_sender(email):
        # A machine relay's domain label (e.g. "ansmtp" from
        # no-reply@ansmtp.ariba.com, or "adobesign"/"concursolutions" from
        # senders that don't happen to start with "no-reply") is not a
        # supplier name - never guess one.
        return {"affiliation": "external", "affiliation_confidence": "H",
                "affiliation_source": "system_sender", "company": None}
    return {"affiliation": "external", "affiliation_confidence": "H",
            "affiliation_source": "domain_heuristic", "company": _company_from_domain(domain)}


def _name_to_local_part_candidates(name: str) -> list[str]:
    """Deterministic corporate-email-convention guesses for a display name -
    string transforms only, no similarity/fuzzy matching. Returns [] for
    anything that doesn't look like a first+last name (a single token is too
    ambiguous to guess a local part from)."""
    tokens = [re.sub(r"[^a-z]", "", t) for t in name.strip().lower().split()]
    tokens = [t for t in tokens if t]
    if len(tokens) < 2:
        return []
    first, last = tokens[0], tokens[-1]
    return list(dict.fromkeys([
        f"{first}{last}", f"{first}.{last}", f"{first[0]}{last}",
        f"{first[0]}.{last}", f"{first}_{last}", f"{last}{first}",
    ]))


# Generational and professional suffixes (fixed 2026-07-29): "John Smith" stored
# vs. "John Smith Jr." or "John Smith, PhD" mentioned in a Socrates query used to
# be a guaranteed exact-match miss - the suffix, not a name difference, broke the
# lookup. Comma-optional (", Jr." and " Jr." both common), trailing-period-optional.
_NAME_SUFFIX_RE = re.compile(
    r",?\s+(?:jr|sr|ii|iii|iv|v|phd|md|esq)\.?$", re.IGNORECASE)


def _normalize_person_name(name: str) -> str:
    """Shared key-builder for both sides of bare-name resolution (index build
    AND lookup) - kept as one function so they can't drift out of sync with
    each other, same discipline as workgraph_signals._SYSTEM_SENDER."""
    collapsed = re.sub(r"\s+", " ", name.strip().lower())
    return _NAME_SUFFIX_RE.sub("", collapsed).strip()


def _build_party_indexes() -> tuple[dict, dict]:
    """Two in-memory indexes over every known party, built once per run() -
    by exact display_name (lowercased, suffix-stripped), and by email
    local-part (lowercased, the part before '@'). Personal-scale table, so a
    full fetch is cheap and far simpler than a query per candidate name."""
    by_display_name: dict[str, list[dict]] = {}
    by_local_part: dict[str, list[dict]] = {}
    for p in ws.list_all_parties():
        if p.get("display_name"):
            key = _normalize_person_name(p["display_name"])
            by_display_name.setdefault(key, []).append(p)
        local = p["primary_email"].split("@", 1)[0].lower()
        by_local_part.setdefault(local, []).append(p)
    return by_display_name, by_local_part


def _resolve_bare_name(name: str, by_display_name: dict, by_local_part: dict) -> Optional[dict]:
    """Resolve a bare display name to an EXISTING party, or None (never
    fabricates a new one). See module docstring for the two-step approach and
    why ambiguity means abstain."""
    key = _normalize_person_name(name)
    if not key:
        return None
    exact = by_display_name.get(key)
    if exact:
        # Confirmed bug, 2026-07-29: when 2+ known parties genuinely share the
        # same display_name (a real, detected collision - e.g. two different
        # "John Smith"s, one internal, one at another supplier), this used to
        # just fall through to the WEAKER local-part guess below instead of
        # abstaining - which could then confidently resolve to a THIRD,
        # unrelated party, worse than either real match. An ambiguous exact
        # match must abstain immediately, same discipline the local-part step
        # already applies to itself.
        return exact[0] if len(exact) == 1 else None

    matched: dict[str, dict] = {}
    for cand in _name_to_local_part_candidates(name):
        for p in by_local_part.get(cand, []):
            matched[p["id"]] = p
    if len(matched) == 1:
        return next(iter(matched.values()))
    return None


def _party_id_for(email: str) -> str:
    """Stable, readable id derived from the email's local part + domain
    initial - collisions are resolved by upsert_party's UNIQUE(primary_email)
    constraint doing the real identity work; this id is just a label."""
    safe = re.sub(r"[^a-z0-9]+", "-", email.lower()).strip("-")
    return f"party-{safe[:60]}"


def extract_and_link_parties_for_issue(issue_id: str) -> dict:
    """Reads every raw_item linked to this issue, upserts a Party per valid
    email-looking from_actor/participant, and links each to the issue. A bare
    display name is first attempted against _resolve_bare_name before falling
    back to skipped_name_only (see module docstring). Safe to re-run -
    upsert_party and link_party_to_issue are both idempotent.

    Rebuilds the party name/local-part index fresh on every call (a full
    table fetch - personal-scale, hundreds of rows, not worth threading a
    shared snapshot through run()'s batch loop) so a party created earlier
    in the SAME batch is already resolvable by a later issue in that batch,
    not just on the next run."""
    import json as _json

    by_display_name, by_local_part = _build_party_indexes()

    items = ws.get_raw_items_for_issue(issue_id)
    linked = 0
    resolved_by_name = 0
    skipped_name_only = 0
    seen_emails = set()

    for item in items:
        candidates = []
        if item.get("from_actor"):
            candidates.append(item["from_actor"])
        try:
            candidates.extend(_json.loads(item.get("participants") or "[]"))
        except Exception:
            pass

        for raw in candidates:
            raw = (raw or "").strip()
            if not raw:
                continue
            if not _looks_like_email(raw):
                resolved = _resolve_bare_name(raw, by_display_name, by_local_part)
                if resolved is None:
                    skipped_name_only += 1
                    continue
                # Teach the party its display_name for next time (upsert_party
                # only fills it in if not already set - never overwrites).
                ws.upsert_party(
                    id=resolved["id"], primary_email=resolved["primary_email"], display_name=raw,
                    affiliation=resolved["affiliation"], affiliation_confidence=resolved["affiliation_confidence"],
                    affiliation_source=resolved["affiliation_source"], company=resolved.get("company"),
                )
                ws.link_party_to_issue(issue_id, resolved["id"])
                resolved_by_name += 1
                continue
            email = raw.lower()
            if email in seen_emails:
                continue
            seen_emails.add(email)

            info = classify_affiliation(email)
            ws.upsert_party(
                id=_party_id_for(email), primary_email=email, display_name=None,
                affiliation=info["affiliation"], affiliation_confidence=info["affiliation_confidence"],
                affiliation_source=info["affiliation_source"], company=info["company"],
            )
            party = ws.get_party_by_email(email)
            if party:
                ws.link_party_to_issue(issue_id, party["id"])
                linked += 1

    return {"issue_id": issue_id, "parties_linked": linked, "resolved_by_name": resolved_by_name,
            "skipped_name_only": skipped_name_only}


def backfill_clear_machine_signal_companies() -> dict:
    """One-time (or on-demand) repair pass: null out `company` on any
    EXISTING party row whose email is a known machine-signal sender - upsert_
    party never re-guesses company for a party that already exists (only
    fills in display_name if missing), so a bad guess made before this fix
    (or before _MACHINE_SIGNAL_DOMAIN existed) sticks forever unless
    corrected here directly. Confirmed live 2026-07-29: 'Ansmtp' (63 issues),
    'Adobesign' (9), 'Concursolutions' (5) were all showing up as real
    external companies in production before this ran."""
    parties = ws.list_all_parties()
    cleared = 0
    for p in parties:
        if p.get("company") and workgraph_signals.is_automated_sender(p["primary_email"]):
            ws.clear_party_company(p["id"], affiliation_source="system_sender")
            cleared += 1
    return {"checked": len(parties), "companies_cleared": cleared}


def run(issue_ids: list) -> dict:
    """Driver for a batch of issue ids (typically cluster_and_link's
    touched_issues set) - not run over the whole DB every time, since that's
    wasted work for issues whose parties were already extracted on a prior
    pass and haven't gained new evidence since."""
    total_linked = 0
    total_resolved_by_name = 0
    total_skipped = 0
    for issue_id in issue_ids:
        result = extract_and_link_parties_for_issue(issue_id)
        total_linked += result["parties_linked"]
        total_resolved_by_name += result["resolved_by_name"]
        total_skipped += result["skipped_name_only"]
    return {"issues_processed": len(issue_ids), "parties_linked": total_linked,
            "resolved_by_name": total_resolved_by_name, "skipped_name_only": total_skipped}


if __name__ == "__main__":
    import json
    ws.init_workgraph()
    all_ids = ws.list_issue_ids()
    print(json.dumps(run(all_ids), indent=2))
