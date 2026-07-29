#!/usr/bin/env python3
"""
symphony_statusline.py — born-worker statusLine hook (Abe, 2026-07-24).

George's ask: give born workers (Tia, Rosie, etc.) a real saturation signal, the same
way aria_canon workers get theirs — via CC's own statusLine hook, which the harness
invokes automatically on every render and pipes a JSON payload to via stdin. That
payload includes a genuine field, context_window.used_percentage — NOT a jsonl-filesize
approximation (aria_sync/tools/statusline.py, the aria_canon original, reads the exact
same field; Abe verified this directly rather than assuming).

Minimal by design: born workers don't need the cost/model/cockpit-rendering machinery
the full aria_canon statusline.py carries — just enough to (a) show something sane in
the terminal status bar and (b) write the sat-heartbeat file cohort_post.py's
_latest_substrate_sat() already reads (/tmp/cohort_sat_sess_<sid>.json, same shape:
worker/ts/pct), so the mtime-gate (once the born-appropriate signal exists) can use it.
"""
import json
import os
import sys


def main() -> None:
    raw = sys.stdin.read() or "{}"
    try:
        d = json.loads(raw)
    except json.JSONDecodeError:
        d = {}

    cw = d.get("context_window") or {}
    pct = round(float(cw.get("used_percentage") or 0), 2)
    tokens = (cw.get("total_input_tokens") or 0) + (cw.get("total_output_tokens") or 0)

    worker = os.environ.get("SYMPHONY_WORKER") or "unresolved"
    sid = (d.get("session_id") if isinstance(d, dict) else "") or ""
    sid_short = sid[:8] if sid else f"pid{os.getpid()}"

    # Terminal status bar — simple bar, no cost/model rendering (born installs don't
    # need the full aria_canon cockpit machinery this feeds).
    filled = int(pct) // 10
    bar = "#" * filled + "-" * (10 - filled)
    print(f"[{worker}] [{bar}] {pct}% · {tokens} tok")

    if worker == "unresolved":
        return  # don't write a heartbeat for an unresolved worker (matches aria_canon's own guard)

    state = {"worker": worker, "ts": __import__("time").time(), "pct": pct,
             "tokens_total": tokens, "session_id": sid[:16]}
    out_path = f"/tmp/cohort_sat_sess_{sid_short}.json"
    tmp_path = out_path + f".tmp.{os.getpid()}"
    try:
        with open(tmp_path, "w") as f:
            json.dump(state, f)
        os.rename(tmp_path, out_path)  # atomic replace
    except Exception:
        pass  # best-effort; a failed heartbeat write shouldn't break the status bar


if __name__ == "__main__":
    main()
