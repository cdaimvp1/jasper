# outlook_draft_reply.ps1 - creates a REAL Outlook draft reply to one
# specific item, by EntryID, and displays it for review - task #47. Uses
# Outlook's own Reply()/ReplyAll(), which returns a new draft MailItem
# already addressed and quoting the original thread - never calls Send().
# Display() puts it on screen exactly like a person clicking Reply
# themselves; nothing here transmits anything.
#
# Usage:
#   powershell -File outlook_draft_reply.ps1 -EntryID "<entry id>" [-ReplyAll]
param(
    [Parameter(Mandatory=$true)][string]$EntryID,
    [switch]$ReplyAll
)

try {
    $outlook = New-Object -ComObject Outlook.Application
    $ns = $outlook.GetNamespace("MAPI")
    $item = $ns.GetItemFromID($EntryID)
    if (-not $item) {
        Write-Error "No item found for that EntryID - it may have been moved, deleted, or the id is stale."
        exit 2
    }
    $draft = if ($ReplyAll) { $item.ReplyAll() } else { $item.Reply() }
    $draft.Display()
    Write-Output '{"ok":true}'
} catch {
    Write-Error "ERROR: $($_.Exception.Message)"
    exit 1
}
