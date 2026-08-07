# outlook_scan_sent.ps1 - deterministic Outlook COM scan of the Sent Items
# folder, no LLM involved.
#
# Task #270 Phase A (2026-08-07): extended to full field parity with
# outlook_scan.ps1 (ConversationID, participants, real attachments, full
# body staging, item_staged_dir) so a sent item can flow through the exact
# same ingest/classify/cluster_and_link pipeline any inbound item does -
# see outlook_com_sent_ingest.py's own module docstring for why. Previously
# this script fed ONLY personal_patterns.py's Phase 2 mining (task #49) and
# stayed deliberately minimal (entry_id/subject/sent_epoch/body_excerpt);
# personal_patterns.mine_sent_mail() keeps working unchanged since it only
# reads those same fields, still present below alongside the new ones.
#
# The attachment/body-staging/hash functions below are DELIBERATE COPIES of
# outlook_scan.ps1's own Save-RealAttachments/Save-FullBody/Get-ShortHash/
# To-Epoch, not a shared .psm1 - outlook_scan.ps1 is Marc's live, working
# inbound-mail path; refactoring it to extract shared functions risks
# breaking it for a benefit (avoiding duplication) that doesn't outweigh
# that risk. Keep both copies in sync by hand if either changes.
#
# Usage:
#   powershell -File outlook_scan_sent.ps1 -SinceEpoch 1785200000 -MaxItems 500 -StagingDir C:\...\_mail_attachments_staging
param(
    [double]$SinceEpoch = 0,
    [int]$MaxItems = 500,
    [string]$StagingDir = "",
    [int]$SyncWaitSeconds = 0
)

# Same UTF-8 fix as outlook_scan.ps1 - the console's OEM codepage doesn't
# round-trip a body containing non-ASCII characters cleanly otherwise.
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$PR_ATTACHMENT_HIDDEN = "http://schemas.microsoft.com/mapi/proptag/0x7FFE000B"
$MaxAttachmentBytes = 25MB

function Get-ShortHash([string]$s) {
    $md5 = [System.Security.Cryptography.MD5]::Create()
    $bytes = $md5.ComputeHash([System.Text.Encoding]::UTF8.GetBytes($s))
    return -join ($bytes[0..7] | ForEach-Object { $_.ToString('x2') })  # 16 hex chars
}

function Save-RealAttachments {
    param($item, [string]$itemDir)
    $result = [ordered]@{ saved = @() }
    if (-not $itemDir -or $item.Attachments.Count -eq 0) { return $result }
    $saved = @()
    foreach ($att in $item.Attachments) {
        try {
            $hidden = $false
            try { $hidden = [bool]$att.PropertyAccessor.GetProperty($PR_ATTACHMENT_HIDDEN) } catch { }
            if ($hidden) { continue }
            if (-not (Test-Path $itemDir)) { New-Item -ItemType Directory -Path $itemDir -Force | Out-Null }
            $safeName = ($att.FileName -replace '[\\/:*?"<>|]', '_')
            if ($safeName.Length -gt 120) {
                $ext = [System.IO.Path]::GetExtension($safeName)
                $stem = [System.IO.Path]::GetFileNameWithoutExtension($safeName)
                $safeName = $stem.Substring(0, 120 - $ext.Length) + $ext
            }
            $destPath = Join-Path $itemDir $safeName
            $att.SaveAsFile($destPath)
            $len = (Get-Item $destPath).Length
            if ($len -gt $MaxAttachmentBytes) { Remove-Item $destPath -Force; continue }
            $saved += [ordered]@{ filename = $att.FileName; staged_path = $destPath; size_bytes = $len }
        } catch { }
    }
    $result.saved = $saved
    return $result
}

function Save-FullBody {
    param($item, [string]$itemDir)
    $result = [ordered]@{ text_file = ""; html_file = "" }
    if (-not $itemDir) {
        [Console]::Error.WriteLine("JASPER_DIAG: body_capture_failed reason=no_item_dir")
        return $result
    }
    if (-not (Test-Path $itemDir)) { New-Item -ItemType Directory -Path $itemDir -Force | Out-Null }
    try {
        $textPath = Join-Path $itemDir "body.txt"
        [System.IO.File]::WriteAllText($textPath, [string]$item.Body, [System.Text.Encoding]::UTF8)
        $result.text_file = "body.txt"
    } catch {
        [Console]::Error.WriteLine("JASPER_DIAG: body_capture_failed field=body error=$($_.Exception.Message -replace '[\r\n]+', ' ')")
    }
    try {
        $htmlPath = Join-Path $itemDir "body.html"
        [System.IO.File]::WriteAllText($htmlPath, [string]$item.HTMLBody, [System.Text.Encoding]::UTF8)
        $result.html_file = "body.html"
    } catch {
        [Console]::Error.WriteLine("JASPER_DIAG: body_capture_failed field=htmlbody error=$($_.Exception.Message -replace '[\r\n]+', ' ')")
    }
    return $result
}

function To-Epoch($dt) {
    return [Math]::Floor(([DateTimeOffset]$dt.ToUniversalTime()).ToUnixTimeSeconds())
}

try {
    $outlookWasRunning = [bool](Get-Process -Name OUTLOOK -ErrorAction SilentlyContinue)
    [Console]::Error.WriteLine("JASPER_DIAG: outlook_was_running=$outlookWasRunning")

    $outlook = New-Object -ComObject Outlook.Application
    $ns = $outlook.GetNamespace("MAPI")

    if ($SyncWaitSeconds -gt 0) {
        [Console]::Error.WriteLine("JASPER_DIAG: sync_wait_seconds=$SyncWaitSeconds")
        Start-Sleep -Seconds $SyncWaitSeconds
    }

    $sent = $ns.GetDefaultFolder(5)  # olFolderSentMail = 5
    $items = $sent.Items
    $items.Sort("[SentOn]", $true)  # newest first

    $count = 0
    foreach ($item in $items) {
        if ($item.Class -ne 43) { continue }  # olMail only (43) - skip meeting responses etc.

        try {
            $sentEpoch = To-Epoch $item.SentOn
            if ($sentEpoch -le $SinceEpoch) { break }  # sorted newest-first: everything after this is older

            # Best-effort participant list: To/CC display names, semicolon-
            # joined by Outlook - no "sender" field to include since Marc is
            # always the sender on his own Sent Items (personal_patterns.py's
            # existing direction-inference for Teams already establishes this
            # "resolve Marc's own identity separately from cue-guessing"
            # pattern; here it's simpler still - the folder itself IS the
            # identity signal).
            $participants = @()
            if ($item.To) { $participants += ($item.To -split ';' | ForEach-Object { $_.Trim() }) }
            if ($item.CC) { $participants += ($item.CC -split ';' | ForEach-Object { $_.Trim() }) }
            $participants = $participants | Where-Object { $_ -and $_.Trim().Length -gt 0 } | Select-Object -Unique

            $itemDir = if ($StagingDir) { Join-Path $StagingDir (Get-ShortHash $item.EntryID) } else { $null }
            $attResult = Save-RealAttachments -item $item -itemDir $itemDir
            $bodyResult = Save-FullBody -item $item -itemDir $itemDir
            $bodyExcerpt = $item.Body.Substring(0, [Math]::Min(2000, $item.Body.Length))

            $obj = [ordered]@{
                conversation_id  = $item.ConversationID
                entry_id         = $item.EntryID
                subject          = $item.Subject
                participants     = $participants
                sent_epoch       = $sentEpoch
                body_preview     = $item.Body.Substring(0, [Math]::Min(500, $item.Body.Length))
                body_excerpt     = $bodyExcerpt  # personal_patterns.mine_sent_mail's original field - kept unchanged
                attachments      = $attResult.saved
                body_text_file   = $bodyResult.text_file
                body_html_file   = $bodyResult.html_file
                item_staged_dir  = $itemDir
            }
            $obj | ConvertTo-Json -Compress -Depth 4
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
