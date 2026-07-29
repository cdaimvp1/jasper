#!/usr/bin/env python3
"""
cohort_post_cli.py — Stdin/file-based CLI wrapper for cohort_post.post()

Avoids zsh-backtick-eating failure when bodies contain backticks. Use this
instead of `python3 -c "from cohort_post import post; post(...)"`.

Usage:
    # Body from stdin
    echo "Hello @george" | python3 cohort_post_cli.py --sender team_builder --to @george

    # Body from file
    python3 cohort_post_cli.py --sender team_builder --to @all --body-file /tmp/post_body.md

    # Heredoc (backticks preserved — they never touch the shell)
    python3 cohort_post_cli.py --sender canon_builder --to @all <<'EOF'
    🟢 Recipe drift audit done. `routing_trx_actuals_source` shipped.
    EOF

Prints canonical_id on stdout. Full JSON to stderr (--quiet to suppress).

Backticks in stdin/file are PRESERVED · they never touch the shell.
"""
import argparse
import json
import sys
import os

# Add team/setup to import path so cohort_post is importable regardless of cwd
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from cohort_post import post, IdentityNotLoadedError  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Cohort post CLI (backtick-safe stdin/file body)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--sender", required=True, help="Worker slug · e.g. team_builder")
    parser.add_argument("--to", default=None, help="Recipient · @all · @<worker> · @george")
    parser.add_argument("--intent", default=None, help="Semantic hint: ack | status | question | handoff")
    parser.add_argument("--reply-to", dest="reply_to", default=None, help="Parent message id (pp_/tr_/m_)")
    parser.add_argument("--project", default=None, help="Explicit project id (e.g. proj_a4cb7ad3b9)")
    parser.add_argument("--george-view", dest="george_view", default=None, help="Collapsed-view summary for george")
    parser.add_argument("--body-file", default=None, help="Read body from this file instead of stdin")
    parser.add_argument("--quiet", action="store_true", help="Suppress full JSON output to stderr")
    parser.add_argument("--bypass-routing-gate", dest="bypass_routing_gate", action="store_true",
                        help="Bypass R1 routing gate · use only for genuine private DM reply to TR @-mention")
    parser.add_argument("--bypass-identity-gate", dest="bypass_identity_gate", action="store_true",
                        help="Bypass L3 identity gate · emergency only · logged")
    parser.add_argument("--cohort-id", dest="cohort_id", default=None,
                        help="Override cohort_id · default derived from sender · valid: "
                             "['aria_canon', 'ir_cohort']")
    parser.add_argument("--bypass-mtime-gate", dest="bypass_mtime_gate", action="store_true",
                        help="Bypass active.md staleness gate · emergency only · logged + reviewed")
    args = parser.parse_args()

    # Read body — file takes precedence over stdin
    if args.body_file:
        with open(args.body_file, encoding="utf-8") as f:
            body = f.read()
    elif not sys.stdin.isatty():
        try:
            sys.stdin.reconfigure(encoding="utf-8")
        except AttributeError:
            pass
        body = sys.stdin.read()
    else:
        sys.stderr.write("ERROR: provide body via stdin pipe or --body-file\n")
        return 2

    if not body.strip():
        sys.stderr.write("ERROR: body is empty\n")
        return 3

    kwargs = {
        "sender": args.sender,
        "body": body,
        "to": args.to,
        "reply_to": args.reply_to,
        "project": args.project,
        "intent": args.intent,
        "george_view": args.george_view,
        "bypass_routing_gate": args.bypass_routing_gate,
        "bypass_identity_gate": args.bypass_identity_gate,
        "cohort_id": args.cohort_id,
        "bypass_mtime_gate": args.bypass_mtime_gate,
    }
    # Drop None values — cohort_post.post() treats None as "not provided" but be explicit
    kwargs = {k: v for k, v in kwargs.items() if v is not None}

    try:
        result = post(**kwargs)
    except IdentityNotLoadedError as e:
        # First-wake friendly: the identity gate refused because doctrine isn't confirmed yet.
        # Show a clean, actionable line instead of a raw Python traceback (born-worker UX).
        sys.stderr.write(
            "NOT POSTED · your identity isn't confirmed yet — this is NORMAL on first wake, "
            "nothing is broken.\n"
            '  Run:  python3 "$TEAM_SCRIPTS_ROOT/cohort_id_load_confirm.py" <your-worker>\n'
            "  (read your doctrine docs with the Read tool first), then re-run this post.\n"
            f"  detail: {e}\n"
        )
        return 3

    # canonical_id on stdout so callers can capture it cleanly
    canonical_id = result.get("canonical_id", "")
    if canonical_id:
        print(canonical_id)

    # Full JSON to stderr for debugging (suppress with --quiet)
    if not args.quiet:
        sys.stderr.write(json.dumps(result, indent=2) + "\n")

    if not result.get("ok", False):
        sys.stderr.write(f"ERROR: post() returned ok=false: {result}\n")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
