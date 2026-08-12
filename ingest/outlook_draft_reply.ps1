# outlook_draft_reply.ps1 - creates a REAL Outlook draft reply to one
# specific item, by EntryID, and displays it for review - task #47. Uses
# Outlook's own Reply()/ReplyAll(), which returns a new draft MailItem
# already addressed and quoting the original thread - never calls Send().
# Display() puts it on screen exactly like a person clicking Reply
# themselves; nothing here transmits anything.
#
# -RefTag (task #36, optional): a plain "Ref: JW-<issue-id>" line prepended
# at the very top of the draft body, above the quoted original - an
# inconspicuous text token, not a hidden header, so it survives any mail
# client/signature/security rewrite. If this draft comes back on a reply,
# workgraph_signals.JASPER_REF_RE picks it back up as a real fallback
# matching signal (workgraph_classify.cluster_and_link).
#
# -BodyFile/-SaveOnly (task #287, proactive drafting; -BodyFile replaces the
# original -Body string argument 2026-08-13, external-review finding #358:
# Windows' CreateProcess has a hard ~32K character total-command-line
# limit, and an unbounded drafted body - a full status report, a long
# stakeholder update - could hit it passed directly as an argument. The
# caller (outlook_actions.py) writes the body to a private temp UTF-8 file
# and passes its path instead): -BodyFile's content prepends real drafted
# text above the quoted thread, same HTMLBody-prepend mechanism as -RefTag.
# -SaveOnly calls MailItem.Save() instead of Display() - the draft lands in
# the Drafts folder without a visible window popping up, for a proactive
# (unattended) call specifically.
#
# Usage:
#   powershell -File outlook_draft_reply.ps1 -EntryID "<entry id>" [-ReplyAll] [-RefTag "JW-marc-308"] [-BodyFile "C:\path\to\body.txt"] [-SaveOnly]
param(
    [Parameter(Mandatory=$true)][string]$EntryID,
    [switch]$ReplyAll,
    [string]$RefTag,
    [string]$BodyFile,
    [switch]$SaveOnly
)

try {
    $Body = if ($BodyFile) { Get-Content -LiteralPath $BodyFile -Raw -Encoding UTF8 } else { $null }
    $outlook = New-Object -ComObject Outlook.Application
    $ns = $outlook.GetNamespace("MAPI")
    $item = $ns.GetItemFromID($EntryID)
    if (-not $item) {
        Write-Error "No item found for that EntryID - it may have been moved, deleted, or the id is stale."
        exit 2
    }
    $draft = if ($ReplyAll) { $item.ReplyAll() } else { $item.Reply() }
    if ($RefTag) {
        # HTMLBody is populated by Reply()/ReplyAll() regardless of the
        # original item's format, so prepending here (rather than .Body)
        # reliably lands above the quoted chain either way.
        $escapedTag = [System.Net.WebUtility]::HtmlEncode("Ref: $RefTag")
        $draft.HTMLBody = "<div style='font-size:11px;color:#888888'>$escapedTag</div>" + $draft.HTMLBody
    }
    if ($Body) {
        $escapedBody = [System.Net.WebUtility]::HtmlEncode($Body) -replace "`n", "<br/>"
        $draft.HTMLBody = "<div>$escapedBody</div><br/>" + $draft.HTMLBody
    }
    if ($SaveOnly) {
        $draft.Save()
    } else {
        $draft.Display()
    }
    Write-Output '{"ok":true}'
} catch {
    Write-Error "ERROR: $($_.Exception.Message)"
    exit 1
}
