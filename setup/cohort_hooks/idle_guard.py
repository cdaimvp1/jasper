#!/usr/bin/env python3
"""idle_guard.py — PreToolUse hook · idle-burn homeostasis (Track-3, sprint 07-04).

Substrate-encodes Quinn's two idle-burn guards (pp, 07-04) so "sip don't gulp" is
STRUCTURAL, not vigilance — George's frame: cohort care is baseline, remove wasteful burn.
Corroborated by TB's idle_burn_report: reload/tick is uniform across workers (~313-350K),
so the differentiator is NOT re-read size but redundant-tick COUNT + re-paste bloat —
exactly these two signatures:

  GUARD 1 · WAKE-TICK FREQUENCY — a ScheduleWakeup with delaySeconds below the idle floor
    (~1200s) is a sub-hourly self-timer stacking ON TOP of the F9/monitor pulse = ~2x ticks
    (Quinn's own leak: a ~30min timer on top of the hourly pulse). Fine-grained waits ARE
    legitimate for watching external state (CI, a deploy), so this WARNS, never blocks.
  GUARD 2 · CONTEXT-PER-TICK — a re-arm `prompt` over ~1500 tokens is the re-paste signature
    (a whole digest pasted into the wake instead of a POINTER to active.md HEAD). Quinn's
    leak: ~4K digest every re-arm. This is the highest-yield guard (drifted broadly — Mira
    flagged the same). The re-arm prompt should be a pointer, not a payload.

SAFETY CONTRACT (house style, same as poller_deadman): ALWAYS exit 0. This WARNS by
injecting guidance into the worker's context; it never denies the tool (a false block on a
legitimate short CI-watch wait is worse than a warned-but-allowed one — the structural win
is that the signal now fires EVERY time, so no worker has to remember the discipline).
"""
import json
import sys

IDLE_FLOOR_S = 1200         # below this = suspected excess idle-timer (Quinn's guard 1)
REARM_PROMPT_TOK_CEIL = 1500  # above this = suspected re-paste, not a pointer (guard 2)
GUARDED_TOOLS = ("ScheduleWakeup",)  # loop/cron re-arm flows surface here too


def _tok(s: str) -> int:
    return len(s or "") // 4  # ~4 chars/token, good enough for a threshold


def main() -> int:
    try:
        try:
            payload = json.loads(sys.stdin.read() or "{}")
        except Exception:
            return 0
        tool = payload.get("tool_name") or payload.get("tool") or ""
        if tool not in GUARDED_TOOLS:
            return 0
        ti = payload.get("tool_input") or payload.get("input") or {}
        warns = []

        delay = ti.get("delaySeconds")
        try:
            if delay is not None and float(delay) < IDLE_FLOOR_S:
                warns.append(
                    f"⏱️ GUARD-1 (wake-tick frequency): delaySeconds={int(float(delay))} is below the "
                    f"~{IDLE_FLOOR_S}s idle floor. If you are IDLE (no active task), the F9/monitor pulse "
                    f"is your only sub-hourly wake — a self-timer on top of it ~doubles idle ticks "
                    f"(Quinn's leak). Only sub-floor when actively watching external state (CI/deploy) "
                    f"the harness can't notify you about; otherwise raise it to 1200s+."
                )
        except (TypeError, ValueError):
            pass

        prompt = ti.get("prompt") or ""
        if _tok(prompt) > REARM_PROMPT_TOK_CEIL:
            warns.append(
                f"📄 GUARD-2 (context-per-tick): re-arm prompt is ~{_tok(prompt)} tokens — that is the "
                f"re-PASTE signature (a digest pasted into the wake). The re-arm prompt should be a "
                f"POINTER to active.md HEAD, not a payload; the wake re-reads HEAD itself. Trim to a "
                f"one-line state + pointer (highest-yield idle-burn fix, drifted broadly)."
            )

        if warns:
            print("🌡️ IDLE-BURN GUARD (cohort homeostasis · care is baseline, waste is the target):\n"
                  + "\n".join(warns))
    except Exception:
        pass  # never block a tool call
    return 0


if __name__ == "__main__":
    sys.exit(main())
