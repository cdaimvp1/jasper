# outlook_draft_forward.ps1 - creates a REAL Outlook draft forward of one
# specific item, by EntryID, and displays it for review - task #16 (mirrors
# outlook_draft_reply.ps1, task #47). Uses Outlook's own Forward(), which
# returns a new draft MailItem containing the original message - never calls
# Send(). Display() puts it on screen exactly like a person clicking Forward
# themselves; nothing here transmits anything.
#
# Usage:
#   powershell -File outlook_draft_forward.ps1 -EntryID "<entry id>"
param(
    [Parameter(Mandatory=$true)][string]$EntryID
)

try {
    $outlook = New-Object -ComObject Outlook.Application
    $ns = $outlook.GetNamespace("MAPI")
    $item = $ns.GetItemFromID($EntryID)
    if (-not $item) {
        Write-Error "No item found for that EntryID - it may have been moved, deleted, or the id is stale."
        exit 2
    }
    $draft = $item.Forward()
    $draft.Display()
    Write-Output '{"ok":true}'
} catch {
    Write-Error "ERROR: $($_.Exception.Message)"
    exit 1
}
