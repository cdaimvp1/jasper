"""
workgraph_redline.py — enhancement idea panel #19 (Contract clause-diff/
redline comparison, worker capability): a deterministic, zero-LLM
paragraph-level diff between two attachments' already-extracted text
(attachment_extract.py, task #29/E6).

Deliberately no "which two attachments are versions of each other"
discovery - the caller (a human, or a worker acting on the human's
explicit instruction) picks both attachment ids directly. artifact_
lineages/artifact_versions' own linking only ever proves byte-identical
duplicates (see create_artifact_version's own docstring - "nothing
today can tell a redline from an identical copy from content alone, so
a role beyond the default is only ever set by a FUTURE real producer");
a genuinely edited revision has a different sha256 and was never going
to land in the same lineage under that mechanism. Guessing a
relationship from filename similarity or upload proximity would be
exactly the kind of fuzzy inference this codebase has repeatedly burned
itself on (see workgraph_deadlines.py's own Ariba-expiration-date
caution) - safer to let the human name both documents explicitly.

This is also structurally distinct from, and does not depend on, the
separate (and still Marc-gated) claudeskills docx-tracked-changes
integration (task #112) - that produces a real Word redline .docx via
an external Skill; this is a plain-text structural diff Jasper can
compute itself, right now, from text already sitting in the DB.

Paragraph-level, not sentence/word/character-level - a contract clause
is a paragraph, and diffing at that granularity keeps a changed word's
surrounding clause fully visible in context, rather than a character-
level diff that's technically correct but unreadable. Splits on a
SINGLE newline, not a blank-line double-newline: confirmed live
against real attachments that attachment_extract.py's own extractors
join paragraphs/lines with exactly one `\n` (extract_docx_text: "every
paragraph's text runs, newline-joined between paragraphs"; extract_pdf_
text: pypdf's own per-page extract_text, page-joined the same way) -
splitting on `\n\n` instead found zero paragraph boundaries in any real
document and silently diffed two whole documents as a single "replaced"
block, which is correct but useless as a clause-level diff.
"""
from __future__ import annotations

import difflib

import workgraph_store as ws


def _paragraphs(text: str) -> list[str]:
    if not text:
        return []
    raw = [p.strip() for p in text.replace("\r\n", "\n").split("\n")]
    return [p for p in raw if p]


def compare_attachments(attachment_id_a: int, attachment_id_b: int) -> dict:
    """Raises ValueError (never a silent empty diff) if either
    attachment doesn't exist or has no extracted text yet - "nothing to
    compare" must read as a clear error, not as "no changes found."""
    att_a = ws.get_attachment(attachment_id_a)
    att_b = ws.get_attachment(attachment_id_b)
    if att_a is None:
        raise ValueError(f"no such attachment: {attachment_id_a}")
    if att_b is None:
        raise ValueError(f"no such attachment: {attachment_id_b}")
    text_a, text_b = att_a.get("extracted_text"), att_b.get("extracted_text")
    if not text_a or not text_b:
        raise ValueError("one or both attachments have no extracted text yet")

    paras_a, paras_b = _paragraphs(text_a), _paragraphs(text_b)
    matcher = difflib.SequenceMatcher(a=paras_a, b=paras_b, autojunk=False)

    added: list[str] = []
    removed: list[str] = []
    changed: list[dict] = []
    unchanged_count = 0
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            unchanged_count += i2 - i1
        elif tag == "insert":
            added.extend(paras_b[j1:j2])
        elif tag == "delete":
            removed.extend(paras_a[i1:i2])
        elif tag == "replace":
            old_block, new_block = paras_a[i1:i2], paras_b[j1:j2]
            for k in range(max(len(old_block), len(new_block))):
                old = old_block[k] if k < len(old_block) else None
                new = new_block[k] if k < len(new_block) else None
                if old is None:
                    added.append(new)
                elif new is None:
                    removed.append(old)
                else:
                    changed.append({"old": old, "new": new})

    return {
        "attachment_id_a": attachment_id_a, "attachment_id_b": attachment_id_b,
        "filename_a": att_a.get("filename"), "filename_b": att_b.get("filename"),
        "unchanged_count": unchanged_count, "added": added, "removed": removed, "changed": changed,
        "identical": not added and not removed and not changed,
    }
