# outlook_mark_read.ps1 - marks one specific Outlook item, by EntryID, as
# read - task #275 (closure-triggered mark-as-read). Sets UnRead=$false and
# saves; never deletes, moves, or archives the item - that's the whole scope
# Marc asked for. Same real Outlook COM mechanism as outlook_open_item.ps1
# (GetItemFromID), just one property write instead of Display().
#
# Usage:
#   powershell -File outlook_mark_read.ps1 -EntryID "<entry id>"
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
    if ($item.UnRead) {
        $item.UnRead = $false
        $item.Save()
    }
    Write-Output '{"ok":true}'
} catch {
    Write-Error "ERROR: $($_.Exception.Message)"
    exit 1
}
