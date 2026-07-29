#!/usr/bin/env python3
"""Symphony born-wake identity injection (option B mechanism · Abe).

Run by the born body's SessionStart hook. Resolves the waking worker's identity via Coby's
resolver, interpolates Theo/Mira's born §0 template, and emits it as CC SessionStart
`additionalContext` (JSON on stdout) — so a born worker wakes KNOWING who it is + pointed at
its identity stack to READ LIVE (floor-#6: pointers, never baked).

Ownership: mechanism (this script + interpolation) = Abe · §0 content (the .tmpl) = Theo/Mira ·
resolver contract (JSON out) = Coby. §3 = B (resolver-assert gate: identify_failed → fail-closed
branch; READ_EVIDENCE verified by Mira's born-boot confirm-2, no born cohort_id_load_confirm).

Reads env: SYMPHONY_WORKER, CLAUDE_PROJECT_DIR, TEAM_HOME. Best-effort: any failure emits a
loud-but-safe additionalContext, never crashes the wake.
"""
import os, sys, json, subprocess, re

SETUP = os.path.dirname(os.path.abspath(__file__))
TEMPLATE = os.path.join(SETUP, "symphony_born_wake_protocol.md.tmpl")
RESOLVER = os.path.join(SETUP, "resolve_symphony_identity.py")


def _emit(ctx):
    """Emit additionalContext for a SessionStart hook (CC injects it into the session)."""
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "SessionStart", "additionalContext": ctx}}))


def _resolve():
    """Call Coby's resolver __main__ (prints the identity dict as JSON). Returns dict or {}."""
    try:
        out = subprocess.check_output([sys.executable, RESOLVER], text=True, timeout=30,
                                      stderr=subprocess.DEVNULL)
        return json.loads(out) if out.strip() else {}
    except Exception:
        return {}


def _render(tmpl, idy):
    """Interpolate the born §0 template. Drops the template's leading `#` comment header, resolves
    the {IF identify_failed}...{END IF} block, then substitutes {placeholder} fields.
    Unknown/missing fields render as an explicit [unset:<name>] so a gap is VISIBLE, never silent."""
    # strip the leading comment/banner header (lines up to the first '---' rule after it, or the
    # first non-comment content line) — everything the authors marked with a leading '#'/rule.
    lines = tmpl.splitlines()
    body_start = 0
    for i, ln in enumerate(lines):
        s = ln.strip()
        if s and not s.startswith("#") and not set(s) <= set("─-"):
            body_start = i
            break
    text = "\n".join(lines[body_start:]).strip()

    failed = bool(idy.get("identify_failed"))
    # {IF identify_failed} ... {END IF}: keep inner content only when failed, else remove block.
    def _ifblock(m):
        return m.group(1) if failed else ""
    text = re.sub(r"\{IF identify_failed\}(.*?)\{END IF\}", _ifblock, text, flags=re.DOTALL).strip()

    # {placeholder} substitution — only the known field set; leave shell vars ($TEAM_SCRIPTS_ROOT) alone.
    # python_cmd (Tia's live-wake catch, 2026-07-22): a bare "python"/"python3" resolves via PATH -
    # if the user has no system Python (or a different one shadowing it), every wake-time script
    # invocation breaks. Fix: resolve to the born venv's OWN absolute interpreter path (same probe
    # _venv_python() already uses at install time), so wake-time commands never depend on system
    # Python at all. Falls back to the bare OS-conditional name only if the venv python genuinely
    # isn't found (degraded-but-correct, matches this script's best-effort house style).
    idy = dict(idy)
    _py_cmd = "python" if os.name == "nt" else "python3"
    try:
        _team_home = os.environ.get("TEAM_HOME", "")
        if _team_home:
            _venv_dir = os.path.join(os.path.dirname(_team_home), "venv")
            _cands = ((os.path.join(_venv_dir, "Scripts", "python.exe"), os.path.join(_venv_dir, "python.exe"))
                      if os.name == "nt" else
                      (os.path.join(_venv_dir, "bin", "python3"), os.path.join(_venv_dir, "bin", "python")))
            for _c in _cands:
                if os.path.isfile(_c):
                    _py_cmd = "\"%s\"" % _c
                    break
    except Exception:
        pass
    idy.setdefault("python_cmd", _py_cmd)
    FIELDS = ("worker", "cohort", "archetype", "identity_dir", "cohort_identity_dir",
              "role_doc_path", "archetype_doc_path", "python_cmd")
    def _sub(m):
        key = m.group(1)
        if key not in FIELDS:
            return m.group(0)  # not ours (e.g. a stray brace) — leave verbatim
        val = idy.get(key)
        return str(val) if val not in (None, "") else "[unset:%s]" % key
    text = re.sub(r"\{([a-zA-Z_]+)\}", _sub, text)
    return text


def main():
    worker = os.environ.get("SYMPHONY_WORKER", "")
    if not os.path.exists(TEMPLATE):
        _emit("⚠️ SYMPHONY born-wake: §0 template missing at %s — born body may be mis-shipped. "
              "You are %s; load your identity from cohort_substrate before acting." % (TEMPLATE, worker or "<unset>"))
        return
    idy = _resolve()
    if not idy:
        idy = {"identify_failed": True}
    tmpl = open(TEMPLATE, encoding="utf-8").read()
    _emit(_render(tmpl, idy))


if __name__ == "__main__":
    main()
