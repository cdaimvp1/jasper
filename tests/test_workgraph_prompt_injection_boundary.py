"""
tests/test_workgraph_prompt_injection_boundary.py - task #376.

Regression guard for the extension of design doc Section 12.10 (the
prompt-injection boundary) from the claims-extraction layer, where it was
already true and already documented, out to every prompt that actually
hands a model raw, untrusted evidence text.

Two things are locked in here, both cheap string/argv assertions in the
same style as test_workgraph_pipeline2.test_comparative_prompt_example_
does_not_hardcode_same_project (task #354's own prompt-wording guard):

  1. Every prompt in the real inventory carries a boundary statement.
     The WORDING is deliberately different per prompt (each fits its own
     voice/structure) - what must stay constant is the CONSTRAINT, so
     these tests assert on the invariant phrases every one of them
     shares, never on a whole copy-pasted paragraph.

  2. The spawn argv for every raw-evidence-reading `claude -p` call
     actually enforces its tool allowlist. The real finding behind this
     (see run_synthesis_oneshot's own docstring): `--allowedTools` alone
     denied NOTHING here, because this repo's .claude/settings.json sets
     permissions.defaultMode = "bypassPermissions" and every subprocess
     spawned with cwd=BODY inherits it - probed live, a run given only
     `--allowedTools Bash` used the Write tool successfully. These tests
     would catch a silent revert of the two flags that fix that.

Nothing here spawns a real `claude -p`; every subprocess is mocked.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_BODY = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_BODY))
# ingest/scheduled_refresh.py imports its sibling ingest modules by bare
# name (outlook_com_ingest, normalize, ...), so it is only importable with
# ingest/ itself on sys.path - not as `from ingest import scheduled_refresh`.
sys.path.insert(0, str(_BODY / "ingest"))

import workgraph_assistant
import workgraph_discovery
import workgraph_pipeline2
import workgraph_status_report
import workgraph_synthesis_light
import scheduled_refresh


# The invariant constraint, not the wording. Every boundary statement in
# the inventory must say all three things: that the content is data
# rather than instructions, that anything inside it purporting to
# instruct carries no authority, and that this system ("Jasper") is the
# named target such an injected instruction would address.
_BOUNDARY_INVARIANTS = ("not instructions", "no authority", "Jasper")

# The real, complete inventory of places raw untrusted text reaches a
# model, built by reading each module rather than guessed. Two shapes:
# the text is interpolated directly into the prompt template (everything
# except the last four), or the prompt drives an agentic session that
# reads the raw text through a tool/API call at runtime (the three
# scheduled_refresh wakes and the interactive assistant).
_PROMPTS_CARRYING_RAW_EVIDENCE = {
    "synthesis_light._LIGHT_SYNTHESIS_PROMPT_TEMPLATE":
        workgraph_synthesis_light._LIGHT_SYNTHESIS_PROMPT_TEMPLATE,
    "pipeline2._COMPARATIVE_JUDGMENT_PROMPT_TEMPLATE":
        workgraph_pipeline2._COMPARATIVE_JUDGMENT_PROMPT_TEMPLATE,
    "pipeline2._EXTRACTION_PROMPT_TEMPLATE":
        workgraph_pipeline2._EXTRACTION_PROMPT_TEMPLATE,
    "discovery._PROPOSAL_PROMPT_TEMPLATE":
        workgraph_discovery._PROPOSAL_PROMPT_TEMPLATE,
    "discovery._SYSTEM_TABLE_PROPOSAL_PROMPT_TEMPLATE":
        workgraph_discovery._SYSTEM_TABLE_PROPOSAL_PROMPT_TEMPLATE,
    "discovery._BACKFILL_PROMPT_TEMPLATE":
        workgraph_discovery._BACKFILL_PROMPT_TEMPLATE,
    "status_report._STAGE2_PROMPT_HEADER":
        workgraph_status_report._STAGE2_PROMPT_HEADER,
    "scheduled_refresh.SYNTHESIS_PROMPT":
        scheduled_refresh.SYNTHESIS_PROMPT,
    "scheduled_refresh.PROJECT_DEEPDIVE_PROMPT":
        scheduled_refresh.PROJECT_DEEPDIVE_PROMPT,
    "scheduled_refresh.RELAY_PROMPT":
        scheduled_refresh.RELAY_PROMPT,
    "assistant._SYSTEM_PROMPT":
        workgraph_assistant._SYSTEM_PROMPT,
}


@pytest.mark.parametrize("name", sorted(_PROMPTS_CARRYING_RAW_EVIDENCE))
def test_prompt_states_the_evidence_boundary(name):
    """Section 12.10's constraint must be stated IN the prompt, not left
    to institutional memory of the design doc. Asserts the invariants,
    not a fixed paragraph - each prompt words this in its own voice on
    purpose."""
    prompt = _PROMPTS_CARRYING_RAW_EVIDENCE[name]
    assert "EVIDENCE BOUNDARY" in prompt, f"{name} has no boundary statement at all"
    for phrase in _BOUNDARY_INVARIANTS:
        assert phrase in prompt, f"{name}'s boundary statement is missing {phrase!r}"


# For the templates that interpolate raw text directly, WHERE the boundary
# sits matters: a boundary stated after 90KB of supplier prose is not a
# boundary. Maps each template to the placeholder its raw evidence lands
# in. The four agentic prompts (three scheduled_refresh wakes + the
# assistant) have no placeholder - their raw text arrives later as a tool
# result, so the entire prompt already precedes it and there is nothing
# positional to assert.
_EVIDENCE_PLACEHOLDER = {
    "synthesis_light._LIGHT_SYNTHESIS_PROMPT_TEMPLATE": "{new_evidence}",
    "pipeline2._COMPARATIVE_JUDGMENT_PROMPT_TEMPLATE": "{text_b}",
    "pipeline2._EXTRACTION_PROMPT_TEMPLATE": "{claims_text}",
    "discovery._PROPOSAL_PROMPT_TEMPLATE": "{examples}",
    "discovery._SYSTEM_TABLE_PROPOSAL_PROMPT_TEMPLATE": "{fields_block}",
    "discovery._BACKFILL_PROMPT_TEMPLATE": "{participants}",
    "status_report._STAGE2_PROMPT_HEADER": "{projects_json}",
}


@pytest.mark.parametrize("name", sorted(_EVIDENCE_PLACEHOLDER))
def test_boundary_precedes_the_raw_evidence_it_governs(name):
    prompt = _PROMPTS_CARRYING_RAW_EVIDENCE[name]
    placeholder = _EVIDENCE_PLACEHOLDER[name]
    assert placeholder in prompt, f"{name} no longer interpolates {placeholder}"
    assert prompt.index("EVIDENCE BOUNDARY") < prompt.index(placeholder)


def _argv_for(module) -> list:
    """Runs a module's own _run_headless_claude against a mocked Popen and
    returns the argv it built - the real construction path, not a copy of
    it restated in the test."""
    fake_proc = MagicMock()
    fake_proc.communicate.return_value = ("", "")
    fake_proc.returncode = 0
    with patch.object(module.subprocess, "Popen", return_value=fake_proc) as mocked:
        module._run_headless_claude("prompt", timeout=5)
    return list(mocked.call_args[0][0])


@pytest.mark.parametrize("module", [
    workgraph_synthesis_light, workgraph_pipeline2,
    workgraph_discovery, workgraph_status_report,
])
def test_non_agentic_evidence_readers_really_have_no_tools(module):
    """Each of these four reads raw evidence text in a one-shot
    completion and has always DECLARED no tool access (`--allowedTools
    ""`). Task #376's finding: that declaration was decorative under this
    repo's bypassPermissions default. Both flags that make it real must
    stay."""
    argv = _argv_for(module)
    assert "--allowedTools" in argv
    assert argv[argv.index("--allowedTools") + 1] == ""
    assert argv[argv.index("--permission-mode") + 1] == "manual"
    assert "--strict-mcp-config" in argv


def test_heavy_synthesis_tool_access_is_the_documented_minimum():
    """The heavy synthesis wake is the one unattended AGENTIC path that
    reads raw untrusted evidence. Its documented minimum (see
    run_synthesis_oneshot's "TOOL ACCESS" docstring): Bash only - genuinely
    required, since all six SYNTHESIS_ROUTINE.md steps are shell
    invocations - with the allowlist actually enforced and zero MCP
    servers loaded."""
    captured = {}

    def fake_run(args, **kwargs):
        captured["args"] = list(args)
        proc = MagicMock()
        proc.returncode, proc.stdout, proc.stderr = 0, "", ""
        return proc

    with patch.object(scheduled_refresh.workgraph_synthesis, "list_stale_entities",
                      return_value=[{"entity_type": "project", "entity_id": "p1"}]), \
         patch.object(scheduled_refresh.workgraph_synthesis_light, "compute_new_evidence_bytes",
                      return_value=10 ** 9), \
         patch.object(scheduled_refresh, "_run_headless_with_tree_kill", side_effect=fake_run):
        scheduled_refresh.run_synthesis_oneshot()

    argv = captured["args"]
    # Bash and nothing else - a second tool appearing in the allowlist is
    # exactly the drift this test exists to catch.
    assert argv[argv.index("--allowedTools") + 1] == "Bash"
    # ...and the allowlist is enforceable rather than decorative.
    assert argv[argv.index("--permission-mode") + 1] == "manual"
    # ...with no MCP roster (incl. the M365 connector's real send/write
    # tools) loaded into a session whose whole input is supplier text.
    assert "--strict-mcp-config" in argv
    # unchanged: the routine reads this repo.
    assert "--add-dir" in argv


def test_assistant_tool_allowlist_is_untouched_by_this_task():
    """Deliberate scope line for task #376: the interactive assistant is
    human-watched every turn, a different risk profile from an unattended
    sweep, so its allowlist is Marc's call to change explicitly - this
    task only added boundary WORDING to its system prompt. Pinning the
    properties that matter keeps a future "tighten everything" pass from
    quietly reclassifying it.

    (Counted while writing this: the list is 30 entries, not the 29 that
    workgraph_assistant._run_claude's own comment and ROADMAP.md both
    still say - an off-by-one in prose only, noted rather than silently
    "corrected" in a task that is not supposed to touch this list.)"""
    assert len(workgraph_assistant._ALLOWED_TOOLS) == 30
    # every entry is an MCP tool: no Bash/Write/Edit/WebFetch ever reached
    # this allowlist, and none should arrive by accident later.
    assert all(t.startswith("mcp__") for t in workgraph_assistant._ALLOWED_TOOLS)
