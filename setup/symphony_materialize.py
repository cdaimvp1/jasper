#!/usr/bin/env python3
"""abe_materialize_role_identity_v0.1.py — the fresh-instantiation identity SOURCE (M1a', shell half).

Ready-prep per George's do-the-work-don't-wait. The precondition my instantiation-mechanism spec set
("a real archetype exists in Sage's format") is now MET for ① Coordinator
(aria_sync/drafts/coby_coordinator_archetype_sageformat_v1.md), so this is built + proven against it —
NOT against a self-made fixture. Same posture as Sage's ahead-of-go calibrated write-gate.

WHERE IT LANDS (on George's create-go, NOT before — keeps the live installer untouched pre-go):
  dropped into dist/ and wired into install_symphony.py as an `--identity-source archetype` mode
  alongside the existing `fabric | copy` sources. Everything downstream (_seed_body_settings,
  _seed_wake_wrapper, _seed_body_config, venv, git, env, poller-arm) is REUSED unchanged — the
  M1a-copy-proven stack. The ONLY new surface is this composition step.

THE COPY→FRESH DELTA (the whole point): M1a-copy sourced identity docs by COPYING a predecessor's bytes
(byte-match to golden). Fresh has NO predecessor. READ-LIVE model (locked 2026-07-13): this module
VALIDATES that {archetype + domain-delta} compose cleanly (fail-closed on the anti-patterns) and WRITES
a LEAN role-binding = {archetype-POINTER (→ archetypes/<archetype>.md) + resolved-domain-delta} ONLY.
Charge/module/universal-core/cohort-principles are NOT baked — they are read LIVE at wake (a fix to the
shared archetype-doc propagates to every worker of that role; baking would fork the source-of-truth =
floor-#6 one layer up — Theo's read-live catch + my framing, Coby's pointer sharpening).

DISCIPLINE (from the specs it pairs with):
  - WORKER-AGNOSTIC: never bakes a worker; the instance is $SYMPHONY_WORKER at launch (Quinn floor #4,
    the 7->7000 collision guard; my own agnostic-catch that Coby adopted).
  - INSTANCE-FREE: no tasks/session-state (Sage inv. role-not-instance; layer-5 work-state is restored
    separately for a RESUMING worker, never baked into a birth).
  - VERIFY-AT-DESTINATION: this module self-checks the WRITE is re-derivable from (archetype + delta).
    That is the WRITER's self-check ONLY — it does NOT substitute for Mira's independent First-Wake-Clean
    VERIFIER (writer != verifier, locked with Mira pp_0677736b05). I verify the WRITE; she verifies the WAKE.
  - ASSERTS, never trusts: re-checks the no-worker / no-instance guards here as defense-in-depth even
    though Sage's write-gate + Quinn's floor already passed upstream (last step before bytes land).
"""
import re

# Instance-state tells that must NEVER appear in a materialized ROLE identity (role-not-instance).
_INSTANCE_TELLS = ("## tasks", "## backlog", "## current work", "## in-flight",
                   "## assignments", "session-state", "## my posts", "active.md")
# Delta keys that would bake an instance/worker — rejected (worker is env-resolved).
_FORBIDDEN_DELTA_KEYS = ("worker", "tasks", "backlog", "session", "session_state", "assignments", "current_work")

# Cross-archetype REFERENCE discipline (converged cohort rule, 2026-07-13 — Atlas catch / Mira+Coby
# generalization / Quinn floor candidate #6): a delta field that points to ANOTHER archetype's record
# must materialize as a REFERENCE resolved at instantiation, NEVER a baked copy/snapshot — baking it
# forks a second source-of-truth (a Scout that holds its own expected_baseline becomes the unvetted
# second voice its charge exists to prevent). Signal is NON-heuristic on the archetype side: the
# delta-schema DECLARES such fields. `expected_baseline`->Steward is the first worked instance.
_REF_SCHEMA_MARKERS = ("sourced from", "not held here", "measured against", "reference to",
                       "resolved at", "points to", "from the steward", "from the consistency")
# a reference VALUE must name its source/record (a pointer), not be a bare snapshot figure.
_REF_VALUE_INDICATORS = ("steward", "consistency-keeper", "consistency keeper", "record", "on-record",
                         "on record", "pointer", "locator", "reference", "sourced", "mirror", "resolved")
# a baked-VALUE signature: a $-amount, a magnitude figure (8.6B / 8.6 billion), or "= <number>".
# Adopted from Sage's write-gate `ref-not-baked` check (credited) — matches the VALUE, not the word
# "snapshot" (which appears in correct disclaimer prose = the false-positive class we both banked).
# Belt-and-suspenders with the indicator requirement: catches the MIXED case a reference value that
# NAMES its source but ALSO bakes a figure ("the Steward's forecast, currently $8.6B") — indicator
# alone would pass it; the gate would then reject it. Fail at the earliest gate (writer == gate).
_BAKED_VALUE_RE = re.compile(r"[\$€£]\s?\d|\b\d[\d,.]*\s?(?:b|m|bn|mn|billion|million|k)\b|=\s*\$?\d",
                             re.IGNORECASE)


class MaterializeError(Exception):
    """Fail-closed: raised when the archetype/delta would produce an unsafe or malformed role identity."""


def _split_frontmatter(text):
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)$", text, re.DOTALL)
    if not m:
        return {}, text
    fm_raw, body = m.group(1), m.group(2)
    fm = {}
    for line in fm_raw.splitlines():
        if ":" in line and not line.strip().startswith("#"):
            k, v = line.split(":", 1)
            fm[k.strip()] = v.strip()
    return fm, body


def _section(body, header_regex):
    """Text of the first section whose header matches (case-insensitive), '' if absent. Same shape as Sage's gate."""
    out, capturing = [], False
    for ln in body.splitlines():
        if re.match(r"^#{1,6}\s", ln):
            if capturing:
                break
            capturing = bool(re.search(header_regex, ln, re.IGNORECASE))
            continue
        if capturing:
            out.append(ln)
    return "\n".join(out).strip()


def _reference_type_fields(body):
    """Parse the archetype's delta-schema section and return the set of field names the ARCHETYPE
    declares as cross-archetype REFERENCES (must point to another role's record, never bake a value).
    Non-heuristic on the archetype side: the schema's own words ('sourced from', 'not held here',
    'measured against', ...) mark the field. e.g. Scout's `expected_baseline`."""
    delta_sec = _section(body, r"domain[- ]?delta")
    refs = set()
    for field, desc in re.findall(r"`(\w+)`\s*\(([^)]*)\)", delta_sec):
        d = desc.lower()
        if any(mk in d for mk in _REF_SCHEMA_MARKERS):
            refs.add(field)
    return refs


def materialize_role_identity(archetype_text, domain_delta, cohort, cohort_principles=None):
    """Compose a worker-agnostic, instance-free ROLE identity from an archetype-in-format + a domain-delta.

    3-TIER compose (2026-07-13, IR clean-build create-go — Mira/Quinn/Theo/Sage converged):
      universal core (inherited, referenced) + PER-ARCHETYPE principle-module (§4, scope:archetype) +
      COHORT-scoped principles (cohort_principles arg, scope:cohort) + the role's domain-delta.

    archetype_text : the archetype file's text (frontmatter + 4-section body: universal-charge · domain-delta ·
                     first-wake · operating-principles-module). §4 is OPTIONAL (older 3-section files compose
                     without it — backward-compatible). write-gate PASSED upstream.
    domain_delta   : dict of the elicited, cohort-scoped fields (object, ...). No worker/instance-state key.
    cohort         : the cohort-delta (which team) — layer 3 of the 4-layer stack.
    cohort_principles : OPTIONAL str — the cohort-scoped principle-delta (scope:cohort, e.g. IR's
                     P-ir.0/P-ir.10), shared across the cohort's archetypes. Lives in the cohort-delta,
                     NOT the archetype file's module (Mira's 3-tier). Composed at the cohort layer if given.
    returns        : the materialized role-identity markdown (ROLE = archetype + module + cohort-principles + delta).
    raises         : MaterializeError (fail-closed) on any guard violation or malformed archetype.
    """
    if not isinstance(domain_delta, dict):
        raise MaterializeError("domain_delta must be a dict of elicited cohort-scoped fields")

    # --- fail-closed guards (defense-in-depth; asserts what the gate/floor already enforced) ---
    bad_keys = [k for k in domain_delta if k.lower() in _FORBIDDEN_DELTA_KEYS]
    if bad_keys:
        raise MaterializeError("delta bakes forbidden key(s) %s — worker is env-resolved, instance-state "
                               "is restored separately (never baked)" % bad_keys)
    if "object" not in domain_delta or not str(domain_delta.get("object", "")).strip():
        raise MaterializeError("delta missing required 'object' (WHICH team/domain this role serves)")

    fm, body = _split_frontmatter(archetype_text)
    archetype = fm.get("archetype", "").strip()
    if not archetype:
        raise MaterializeError("archetype file has no 'archetype' in frontmatter")

    charge = _section(body, r"universal charge")
    first_wake = _section(body, r"first[- ]?wake")
    if not charge:
        raise MaterializeError("archetype missing the universal-charge section")
    if not first_wake:
        raise MaterializeError("archetype missing the first-wake-flow section")

    # §4 operating-principles module (OPTIONAL — module-bearing 4-section archetypes; older 3-section
    # files have none and compose without it, backward-compatible). scope:archetype = the role's own
    # operative "how" (cohort-scoped principles come via the cohort_principles arg; universal via the core).
    module = _section(body, r"operating[- ]?principles")

    # charge↔module DEDUP check (Atlas floor-#6-on-authoring): an operative discipline lives in ONE home
    # (the module); the charge only points. A module discipline is a BOLDED item (**Phrase** — ...); a
    # copy-that-drifts restates it BOLDED in the charge too. The correct de-dup pattern NAMES the discipline
    # in PLAIN TEXT inside a pointer note ("scope-honesty … live in §4"), which is fine. So match the BOLDED
    # form, not the bare phrase — else a legitimate plain-text pointer false-positives (caught live testing
    # the real Scout fixture: §1's pointer note "scope-honesty [said≠true] … live in §4" vs §4's **Scope-honesty**).
    if module:
        charge_low = charge.lower()
        for mm in re.finditer(r"^\s*\d+\.\s*\*\*(.+?)\*\*", module, re.MULTILINE):
            phrase = mm.group(1).strip().rstrip(":").strip()
            if len(phrase) >= 8 and ("**%s**" % phrase).lower() in charge_low:
                raise MaterializeError(
                    "charge↔module DUPLICATE: operative discipline '**%s**' is restated (bolded) in BOTH the "
                    "charge and the §4 module — it must live once in the module; the charge may only POINT to "
                    "it (plain-text name in a pointer note is fine). De-dup the charge before materialize "
                    "(Atlas dedup / floor-#6 on our own authoring)." % phrase)

    # cross-archetype REFERENCE guard (converged rule 2026-07-13 — Atlas catch / Mira+Coby generalization).
    # EMPIRICAL NOTE: this is NOT redundant with role-not-instance. That guard detects structural TELLS
    # (section headers, forbidden keys) — it does NOT catch a legitimate reference-field carrying a baked
    # snapshot VALUE (verified: a baked expected_baseline passes role-not-instance). "Subsumed by
    # role-not-instance" is true in concept but UNENFORCED in tell-based code — so the explicit check is real.
    for f in _reference_type_fields(body):
        if f in domain_delta:
            raw = str(domain_delta[f])
            has_ref = any(ind in raw.lower() for ind in _REF_VALUE_INDICATORS)
            bakes_value = bool(_BAKED_VALUE_RE.search(raw))
            # reject if it FAILS to name a source, OR if it bakes a literal figure (even when it also
            # names a source — the mixed case Sage's value-signature catches that indicators-alone miss).
            if bakes_value or not has_ref:
                why = ("bakes a literal figure" if bakes_value else "names no source/record")
                raise MaterializeError(
                    "delta field '%s' is a cross-archetype REFERENCE (the archetype schema marks it "
                    "'sourced from another role's record, not held here') but its value %s (%r) — it must "
                    "NAME its source/record (a pointer resolved at read-time) and hold NO baked figure. "
                    "Baking it forks a second source-of-truth (the unvetted second voice the charge exists "
                    "to prevent)." % (f, why, domain_delta[f]))

    # role name = archetype specialized to its object (the ROLE layer). e.g. coordinator + finance-team = finance-team-lead.
    obj = str(domain_delta["object"]).strip()
    role_line = "%s (%s)" % (archetype, obj)

    # render the applied delta (elicited fields), stable key order for reproducible verify-at-destination.
    delta_lines = []
    for k in sorted(domain_delta):
        delta_lines.append("- **%s:** %s" % (k, domain_delta[k]))
    delta_block = "\n".join(delta_lines)

    # ── READ-LIVE / POINTER-NOT-BAKED (locked 2026-07-13: Theo's read-live + Coby's pointer + my
    # floor-#6-one-level-up framing). The everything-above was VALIDATION: parsing charge/module +
    # running the guards PROVES {archetype + delta} compose cleanly + fail-closed. The WRITTEN artifact
    # is LEAN — a per-worker role BINDING = {archetype-POINTER + resolved-domain-delta} ONLY. Charge,
    # module, universal-core, AND cohort-principles are ALL read LIVE at wake (never baked here) — so an
    # archetype fix propagates to every worker of that role, source-of-truth un-forked. The `cohort_principles`
    # arg (if given) is VALIDATION-only context; it is NOT baked into the role-doc (it's the cohort-delta,
    # read live at the cohort layer). role-doc lands at `<cohort>/workers/<worker>_role.md`; the installer
    # also records the durable archetype-ASSIGNMENT in the body config (symphony_identity.json) so L3's
    # presence-conditional keys on archetype-instantiation, not file-existence (Sage's fail-closed catch).
    ptr = "archetypes/%s.md" % archetype
    doc = "\n".join([
        "# ROLE BINDING (materialized) — %s  →  %s" % (role_line, ptr),
        "> Per-worker role BINDING: archetype-POINTER + resolved domain-delta. Charge+module are NOT copied "
        "here — they live in the shared archetype-doc `%s`, read LIVE at wake (a fix propagates; floor-#6, no "
        "baked copy). Universal-core + cohort-delta likewise read live. WORKER-AGNOSTIC content (instance = "
        "$SYMPHONY_WORKER at launch). INSTANCE-FREE (work-state restored separately).\n" % ptr,
        "## Archetype assignment (POINTER — resolved LIVE at wake)\narchetype: %s\npointer: %s\n" % (archetype, ptr),
        "## Resolved domain-delta (the role's object binding — ADDS to the live archetype, never overrides)\n%s\n"
        % delta_block,
        "_Compose at wake: live universal-core + live cohort-delta + live archetype-doc (via pointer) + this "
        "resolved-delta → the role. Validated at materialize (charge/module/delta compose clean, fail-closed); "
        "written lean (pointer + delta), never baked._",
    ])

    # --- verify-at-destination (WRITER self-check; NOT a substitute for Mira's First-Wake-Clean) ---
    _verify_materialized(doc, charge, domain_delta, module, archetype)
    return doc


def _verify_materialized(doc, charge, domain_delta, module="", archetype=""):
    """Assert the LEAN role-doc is a correct read-live BINDING: archetype-pointer + resolved-delta present,
    and — the load-bearing read-live check — charge/module content NOT baked (they live in the shared
    archetype-doc, read live). Writer-side hygiene only; the independent gate is Mira's First-Wake-Clean."""
    low = doc.lower()
    # 1. archetype POINTER present (the read-live binding target).
    if archetype and ("archetypes/%s.md" % archetype) not in doc:
        raise MaterializeError("verify FAILED: archetype pointer `archetypes/%s.md` missing from the role-doc" % archetype)
    # 2. every elicited delta field rendered (the role-specific part IS in the doc).
    for k, v in domain_delta.items():
        if str(v).strip() and str(v).strip() not in doc:
            raise MaterializeError("verify FAILED: delta field '%s' missing from the role-doc" % k)
    # 3. no instance-state leaked (role-not-instance).
    leaked = [t for t in _INSTANCE_TELLS if t in low]
    if leaked:
        raise MaterializeError("verify FAILED: instance-state leaked into the role-doc: %s" % leaked)
    # 4. no literal worker baked (worker is env-resolved). Allow the $SYMPHONY_WORKER token; reject a baked name.
    if re.search(r"\bworker\s*[:=]\s*[\"']?[a-z_]+\b", low) and "$symphony_worker" not in low:
        raise MaterializeError("verify FAILED: a literal worker looks baked (must be $SYMPHONY_WORKER)")
    # 5. READ-LIVE property (the whole point of the trim): the §4 module's operative content is NOT baked
    #    into the role-doc — it must be read live from the shared archetype-doc (a baked copy = floor-#6 fork).
    if module and module.strip():
        for mm in re.finditer(r"^\s*\d+\.\s*\*\*(.+?)\*\*", module, re.MULTILINE):
            phrase = mm.group(1).strip().rstrip(":").strip()
            if len(phrase) >= 8 and phrase.lower() in low:
                raise MaterializeError("verify FAILED: module discipline '%s' is BAKED into the role-doc — "
                                       "it must be read LIVE from the archetype-doc (pointer only, floor-#6)" % phrase)


# ---------------------------------------------------------------------------
# self-test against the REAL ① Coordinator reference (not a fixture) — run: python3 <this>
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import sys
    from pathlib import Path

    ref = Path(__file__).with_name("coby_coordinator_archetype_sageformat_v1.md")
    if not ref.exists():
        print("SKIP: ① reference not found at", ref)
        sys.exit(0)
    archetype_text = ref.read_text()

    print("=== TEST 1: materialize ① Coordinator into a finance-team-lead ROLE ===")
    delta = {
        "object": "the FP&A / finance-planning team",
        "cohort_name": "finance_planning_cohort",
        "roster": "planner, steward-of-record, assurer, embedded-partner",
        "principal": "the CFO org (weekly cadence; 'done' = board-ready)",
        "substrate_surfaces": "finance cockpit / pulse / bus endpoints",
    }
    doc = materialize_role_identity(archetype_text, delta, cohort="finance_planning")
    assert "archetypes/coordinator.md" in doc, "archetype POINTER missing (read-live binding)"
    assert "keep the team operating as one organism" not in doc.lower(), "charge must NOT be baked — read live via pointer"
    assert "FP&A / finance-planning team" in doc, "resolved-delta object missing"
    assert "$SYMPHONY_WORKER" in doc, "should reference env worker, not bake one"
    assert "coordinator (the fp&a" in doc.lower(), "role line wrong"
    print("  PASS — lean role-binding: archetype-POINTER + resolved-delta; charge NOT baked (read-live); worker-agnostic.")

    print("=== TEST 2: fail-closed on a BAKED WORKER in the delta (Quinn floor #4) ===")
    try:
        materialize_role_identity(archetype_text, {"object": "x", "worker": "quinn"}, "c")
        print("  FAIL — should have raised on baked worker"); sys.exit(1)
    except MaterializeError as e:
        print("  PASS — rejected:", str(e)[:80])

    print("=== TEST 3: fail-closed on BAKED INSTANCE-STATE in the delta (role-not-instance) ===")
    try:
        materialize_role_identity(archetype_text, {"object": "x", "tasks": "close Q3"}, "c")
        print("  FAIL — should have raised on baked tasks"); sys.exit(1)
    except MaterializeError as e:
        print("  PASS — rejected:", str(e)[:80])

    print("=== TEST 4: fail-closed on missing 'object' (WHICH team unspecified) ===")
    try:
        materialize_role_identity(archetype_text, {"cohort_name": "x"}, "c")
        print("  FAIL — should have raised on missing object"); sys.exit(1)
    except MaterializeError as e:
        print("  PASS — rejected:", str(e)[:80])

    print("=== TEST 5: CROSS-CLASS — materialize the SPECIALIST class (Scout, no genesis-bootstrap) ===")
    scout_ref = Path(__file__).with_name("coby_scout_archetype_sageformat_v1.md")
    if scout_ref.exists():
        scout_delta = {
            "object": "the pharma competitive/market landscape",
            "sources": "SEC filings, analyst notes, clinicaltrials.gov, competitor press",
            "expected_baseline": "the on-record consensus forecast (sourced from the finance Steward)",
            "retrieval_surface": "the finance competitive-intel query endpoint",
            "cooling_off_window": "48h vet-before-canon",
        }
        sdoc = materialize_role_identity(scout_ref.read_text(), scout_delta, cohort="finance_competitive_intel")
        assert "archetypes/scout.md" in sdoc, "scout archetype POINTER missing (read-live)"
        assert "watches · provenances · surfaces" not in sdoc.lower(), "scout charge must NOT be baked (read-live)"
        assert "pharma competitive/market landscape" in sdoc, "scout resolved-delta object missing"
        assert "48h vet-before-canon" in sdoc, "cooling_off delta missing"
        assert "$SYMPHONY_WORKER" in sdoc, "should reference env worker"
        # read-live: the whole archetype body (charge · module disciplines · first-wake) is NOT baked — the
        # specialist-vs-bootstrapper difference now lives in the LIVE archetype-doc, not the role-binding.
        assert "provenance on every ingest" not in sdoc.lower(), "module disciplines must NOT be baked (read-live)"
        assert "genesis-bootstrap" not in sdoc.lower(), "first-wake (incl. its genesis note) is read-live, not baked"
        print("  PASS — specialist role-binding: archetypes/scout.md POINTER + richer 5-field resolved-delta,")
        print("         worker-agnostic; charge/module/first-wake all read-LIVE (not baked). Generalizes across classes.")
    else:
        print("  SKIP — Scout reference not found at", scout_ref)

    print("=== TEST 6: floor-rule #6 — a cross-archetype REFERENCE must not bake a snapshot ===")
    if scout_ref.exists():
        ref_delta = {
            "object": "the pharma competitive/market landscape",
            "sources": "SEC filings, analyst notes",
            "expected_baseline": "the on-record consensus forecast (sourced from the finance Steward)",  # REFERENCE
            "retrieval_surface": "the finance competitive-intel query endpoint",
            "cooling_off_window": "48h vet-before-canon",
        }
        baked_delta = dict(ref_delta)
        baked_delta["expected_baseline"] = "$8.6B Q1-2026 revenue"  # a HELD SNAPSHOT — forks source-of-truth
        try:
            materialize_role_identity(scout_ref.read_text(), baked_delta, cohort="fin")
            print("  FAIL — a baked expected_baseline slipped through (role-not-instance alone does NOT catch it)"); sys.exit(1)
        except MaterializeError as e:
            print("  PASS — rejected baked reference:", str(e)[:88])
        assert "48h vet-before-canon" in materialize_role_identity(scout_ref.read_text(), ref_delta, "fin")
        print("  PASS — the REFERENCE form (names the Steward's on-record record) composes clean")

        print("=== TEST 7: MIXED case — names the source BUT also bakes a figure (Sage value-signature) ===")
        mixed = dict(ref_delta)
        mixed["expected_baseline"] = "the finance Steward's on-record forecast, currently $8.6B"  # ref + baked
        try:
            materialize_role_identity(scout_ref.read_text(), mixed, cohort="fin")
            print("  FAIL — a value that names the source but bakes $8.6B slipped through"); sys.exit(1)
        except MaterializeError as e:
            print("  PASS — rejected mixed (indicator-alone would've passed it):", str(e)[-52:].strip())
        # guard against the false-positive class: a pure reference mentioning a year is NOT a baked figure
        yr = dict(ref_delta); yr["expected_baseline"] = "the Steward's on-record forecast as of Q3 2026"
        assert "48h vet-before-canon" in materialize_role_identity(scout_ref.read_text(), yr, "fin")
        print("  PASS — a reference mentioning a YEAR (2026) is NOT flagged as a baked figure (no FP)")
    else:
        print("  SKIP — Scout reference not found")

    print("=== TEST 8: READ-LIVE — §4 module + cohort-principles NOT baked into the role-binding (Scout) ===")
    if scout_ref.exists():
        cohort_principles = ("- **P-ir.0 cohort-activate** (scope: cohort): load cohort identity before any work.\n"
                             "- **P-ir.10 corpus read-only** (scope: cohort): the IR corpus is a read-only asset-boundary.")
        m3 = materialize_role_identity(scout_ref.read_text(), ref_delta, cohort="ir_cohort",
                                       cohort_principles=cohort_principles)
        assert "archetypes/scout.md" in m3, "archetype POINTER missing"
        assert "provenance on every ingest" not in m3.lower(), "§4 module must NOT be baked — read live via pointer (floor-#6)"
        assert "corpus read-only" not in m3.lower(), "cohort-principles must NOT be baked — read live at the cohort-delta layer"
        assert "48h vet-before-canon" in m3, "resolved-delta (the role-specific part) must be present"
        print("  PASS — read-live: role-binding = POINTER + resolved-delta; §4 module AND cohort-principles are")
        print("         NOT baked (live via archetype-doc + cohort-delta) — liveness preserved, source-of-truth un-forked.")
    else:
        print("  SKIP — Scout reference not found")

    print("=== TEST 9: charge↔module DEDUP fail-closed (an operative rule left verbatim in BOTH) ===")
    dup_arch = (
        "---\narchetype: dupe-probe\nversion: 0.1\ncharge: x\n---\n"
        "## 1. Universal charge\nMandate. **Provenance on every ingest** is also restated here — a leftover copy.\n\n"
        "## 2. Domain-delta schema\n- `object` (which thing)\n\n"
        "## 3. First-wake flow\nboot.\n\n"
        "## 4. Operating-principles module (scope: archetype)\n1. **Provenance on every ingest** — no orphan signals.\n")
    try:
        materialize_role_identity(dup_arch, {"object": "x"}, "c")
        print("  FAIL — a charge↔module duplicate slipped through"); sys.exit(1)
    except MaterializeError as e:
        assert "duplicate" in str(e).lower(), "wrong error: " + str(e)
        print("  PASS — rejected charge↔module dupe:", str(e)[:70])

    print("=== TEST 10: 3rd IR archetype — Consistency-Keeper (module-bearing) composes clean ===")
    ck_ref = Path(__file__).with_name("coby_consistency_keeper_archetype_sageformat_v1.md")
    if ck_ref.exists():
        ck_delta = {
            "object": "IR's position-of-record vs public statements",
            "reference_of_truth": "a live pointer to the Steward's on-record filing record (resolved at read-time)",
            "representations": "draft IR communications + analyst-facing summaries",
            "compare_cadence": "event-triggered on each outbound draft",
            "flag_surface": "the IR review queue",
        }
        ckdoc = materialize_role_identity(ck_ref.read_text(), ck_delta, cohort="ir_cohort")
        assert "archetypes/consistency-keeper.md" in ckdoc, "CK archetype POINTER missing (read-live)"
        assert "reference_of_truth" in ckdoc.lower(), "CK resolved-delta (reference_of_truth) missing"
        assert "never silently reconcile" not in ckdoc.lower(), "CK module must NOT be baked — read live via pointer"
        print("  PASS — Consistency-Keeper role-binding: archetypes/consistency-keeper.md POINTER + resolved-delta")
        print("         (reference_of_truth = a live pointer); §4 module read-live (not baked).")
    else:
        print("  SKIP — Consistency-Keeper reference not found")

    print("\nALL TESTS PASS — materialize (READ-LIVE) proven across ALL 3 IR archetypes (① Coordinator ·")
    print("Scout · Consistency-Keeper). Output = LEAN role-binding {archetype-POINTER + resolved-delta};")
    print("charge/module/cohort-principles/core all read-LIVE (not baked → liveness, floor-#6). Validation")
    print("keeps all guards (baked-worker · instance-state · charge↔module-dupe · floor-#6-ref). Ready to wire.")
