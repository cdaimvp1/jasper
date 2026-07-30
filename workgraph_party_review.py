"""
workgraph_party_review.py — task #79: Party resolution confidence review
queue.

Real data investigated before building this: every external party in
this install is already affiliation_confidence='H' - that tier is never
'M' or 'L' for an external party. 'M' is used for INTERNAL parties (an
artifact of how the internal/external call itself is scored, not
something worth Marc's review time - a Lilly-domain sender being
internal isn't actually uncertain). So a queue literally scoped to
"confidence != H" would surface 233 real Lilly colleagues for no
reason.

The genuinely reviewable gap is different: an external party with NO
identified company. That's a real, actionable hole - it means this
contact can't be grouped into a Supplier Dashboard entry, matched by an
Aristotle match_on='supplier' rule, etc. - EXCLUDING automated system-
sender addresses (Ariba/DocuSign/ContractPodAI/AdobeSign notification
senders), which correctly and deliberately have no company; that's not a
gap, it's the right answer for those addresses.
"""
from __future__ import annotations

import workgraph_store as ws


def list_parties_needing_review() -> list[dict]:
    """External parties with no identified company, excluding system-
    sender addresses. Each entry keeps enough context (email, display
    name, affiliation_source) for Marc to correct it via the existing
    correct_party_affiliation API."""
    parties = ws.list_parties(affiliation="external")
    return [
        p for p in parties
        if not p.get("company") and p.get("affiliation_source") != "system_sender"
    ]
