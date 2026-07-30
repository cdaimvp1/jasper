# outlook_scan_sent.ps1 - deterministic Outlook COM scan of the Sent Items
# folder, no LLM involved. Feeds ONLY personal_patterns.py's Phase 2 mining
# (task #49) - deliberately NOT a full ingestion into raw_items/the triage
# work graph (sent mail isn't triage input, it's a personal-learning signal
# about how Marc himself writes), so this stays minimal on purpose: no
# attachment staging, no HTML body, no participant list, nothing written to
# the document library - just enough to keyword-match Marc's own outgoing
# text and dedupe re-scans via a cursor on SentOn.
#
# Usage:
#   powershell -File outlook_scan_sent.ps1 -SinceEpoch 1785200000 -MaxItems 500
param(
    [double]$SinceEpoch = 0,
    [int]$MaxItems = 500
)

# Same UTF-8 fix as outlook_scan.ps1 - the console's OEM codepage doesn't
# round-trip a body containing non-ASCII characters cleanly otherwise.
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

function To-Epoch($dt) {
    return [Math]::Floor(([DateTimeOffset]$dt.ToUniversalTime()).ToUnixTimeSeconds())
}

try {
    $outlook = New-Object -ComObject Outlook.Application
    $ns = $outlook.GetNamespace("MAPI")
    $sent = $ns.GetDefaultFolder(5)  # olFolderSentMail = 5
    $items = $sent.Items
    $items.Sort("[SentOn]", $true)  # newest first

    $count = 0
    foreach ($item in $items) {
        if ($item.Class -ne 43) { continue }  # olMail only (43) - skip meeting responses etc.

        # Per-item try/catch (same pattern as outlook_scan.ps1) - one bad
        # sent item must never lose every other already-valid line.
        try {
            $sentEpoch = To-Epoch $item.SentOn
            if ($sentEpoch -le $SinceEpoch) { break }  # sorted newest-first: everything after this is older

            $obj = [ordered]@{
                entry_id     = $item.EntryID
                subject      = $item.Subject
                sent_epoch   = $sentEpoch
                body_excerpt = $item.Body.Substring(0, [Math]::Min(2000, $item.Body.Length))
            }
            $obj | ConvertTo-Json -Compress -Depth 3
            $count++
            if ($count -ge $MaxItems) { break }
        } catch {
            Write-Warning "skipping one item due to an error: $($_.Exception.Message)"
            continue
        }
    }
} catch {
    Write-Error "ERROR: $($_.Exception.Message)"
    exit 1
}
