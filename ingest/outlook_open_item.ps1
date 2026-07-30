# outlook_open_item.ps1 - opens one specific Outlook item, by EntryID, in a
# real Outlook reading window - task #46. Read+display only: never sends,
# never modifies the item, never marks it read/unread (Display() shows the
# item exactly as GetItemFromID already does for property access elsewhere
# in this codebase, e.g. outlook_com_ingest.py's recovery work this session).
#
# Usage:
#   powershell -File outlook_open_item.ps1 -EntryID "<entry id>"
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
    $item.Display()
    Write-Output '{"ok":true}'
} catch {
    Write-Error "ERROR: $($_.Exception.Message)"
    exit 1
}
