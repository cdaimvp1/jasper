# outlook_scan.ps1 - deterministic Outlook COM scan, no LLM involved.
#
# Scans a named folder (default: "Careful", Marc's real effective inbox - his
# auto-file rule routes all real inbound there; classic Inbox is empty) for
# mail items with ReceivedTime strictly after -SinceEpoch, and emits one JSON
# object per line (JSONL) to stdout. Pure read, no state, no network beyond
# the already-authenticated local Outlook session.
#
# Real (non-inline) attachments are saved to -StagingDir\<short hash>\<filename>
# via Outlook COM's own Attachments.Item.SaveAsFile - Python can't reach into
# a live Outlook session's attachment bytes any other way. The staging
# subfolder is a short hash of EntryID, NOT the raw EntryID (~140 chars) -
# confirmed empirically that the full EntryID plus a real filename (e.g. a
# 70-80 char contract .docx name) blows past Windows' 260-char MAX_PATH and
# SaveAsFile fails silently with "Path does not exist," while short filenames
# like image001.png happened to still fit - this looked like a detection bug
# at first but was a path-length bug. Each JSON line reports the exact
# attachments_staged_dir used, so outlook_com_ingest.py's cleanup step doesn't
# need to re-derive the hash itself.
#
# Usage:
#   powershell -File outlook_scan.ps1 -FolderName "Careful" -SinceEpoch 1785200000 -MaxItems 500 -StagingDir C:\...\_mail_attachments_staging

# -UnreadOnly (2026-07-29, Tia): backlog-sweep mode, added alongside the
# original cursor-based scan rather than replacing it. The original scan
# structurally cannot reach old mail — it sorts newest-first and BREAKS the
# moment it hits an item at/before -SinceEpoch (see the main loop below), so
# anything the cursor has already marched past is permanently invisible to it,
# unread or not. This mode ignores the cursor entirely and asks Outlook's own
# index for "[Unread] = true" directly (Items.Restrict — a native COM filter,
# not a manual walk-and-check of every item, which would be slow against a
# large folder). Read-only: property access via COM does not mark an item
# read (confirmed by the original scan already doing this for years without
# side effects) — nothing here calls .Display() or sets .UnRead.
param(
    [string]$FolderName = "Careful",
    [double]$SinceEpoch = 0,
    [int]$MaxItems = 500,
    [string]$StagingDir = "",
    [switch]$UnreadOnly
)

# Force UTF-8 stdout - the console's OEM codepage (437 on this box) doesn't
# round-trip through Python's subprocess decode (cp1252) cleanly, and a
# mismatch here can silently corrupt an escaped quote inside a body_preview
# into a bare one, breaking that line's JSON and dropping the whole message
# from ingestion (confirmed - caught this exact failure mode while testing).
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

# PR_ATTACHMENT_HIDDEN (0x7FFE000B) - true for inline content (signature
# logos, tracking pixels rendered as part of the HTML body) rather than a
# real attachment a person actually attached. Skip those; keep everything else.
$PR_ATTACHMENT_HIDDEN = "http://schemas.microsoft.com/mapi/proptag/0x7FFE000B"
$MaxAttachmentBytes = 25MB

function Get-ShortHash([string]$s) {
    $md5 = [System.Security.Cryptography.MD5]::Create()
    $bytes = $md5.ComputeHash([System.Text.Encoding]::UTF8.GetBytes($s))
    return -join ($bytes[0..7] | ForEach-Object { $_.ToString('x2') })  # 16 hex chars
}

function Save-RealAttachments {
    param($item, [string]$stagingDir)
    $result = [ordered]@{ saved = @(); dir = "" }
    if (-not $stagingDir -or $item.Attachments.Count -eq 0) { return $result }
    $itemDir = Join-Path $stagingDir (Get-ShortHash $item.EntryID)
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
            if ($len -gt $MaxAttachmentBytes) { Remove-Item $destPath -Force; continue }  # oversized - skip, don't fail the scan
            $saved += [ordered]@{ filename = $att.FileName; staged_path = $destPath; size_bytes = $len }
        } catch { }  # one bad attachment (e.g. an OLE object SaveAsFile can't handle) never fails the whole item
    }
    $result.saved = $saved
    if ($saved.Count -gt 0) { $result.dir = $itemDir }
    return $result
}

function Find-FolderByName {
    param($folder, $name)
    if ($folder.Name -eq $name) { return $folder }
    foreach ($sub in $folder.Folders) {
        $result = Find-FolderByName -folder $sub -name $name
        if ($result) { return $result }
    }
    return $null
}

function To-Epoch($dt) {
    # Outlook COM dates are already local DateTime objects; ToUniversalTime keeps
    # the epoch conversion timezone-correct.
    return [Math]::Floor(([DateTimeOffset]$dt.ToUniversalTime()).ToUnixTimeSeconds())
}

# PR_SENDER_SMTP_ADDRESS (0x5D01001E) - resolves an internal sender's real SMTP
# address instead of the raw Exchange legacyExchangeDN string (/O=EXCHANGELABS/...)
# that SenderEmailAddress returns for on-prem/hybrid Exchange senders. Confirmed
# working against this mailbox (0x39FE001E, the more commonly-documented tag, is
# NOT present here - verified empirically, not assumed). GetExchangeUser() is a
# second, independent confirmation kept as a fallback; raw SenderEmailAddress is
# the last resort (already correct for external senders).
$PR_SENDER_SMTP_ADDRESS = "http://schemas.microsoft.com/mapi/proptag/0x5D01001E"

function Resolve-SenderSmtp($item) {
    try {
        $smtp = $item.PropertyAccessor.GetProperty($PR_SENDER_SMTP_ADDRESS)
        if ($smtp) { return $smtp }
    } catch { }
    try {
        $exUser = $item.Sender.GetExchangeUser()
        if ($exUser -and $exUser.PrimarySmtpAddress) { return $exUser.PrimarySmtpAddress }
    } catch { }
    return $item.SenderEmailAddress
}

try {
    $outlook = New-Object -ComObject Outlook.Application
    $ns = $outlook.GetNamespace("MAPI")

    $target = $null
    foreach ($store in $ns.Folders) {
        $target = Find-FolderByName -folder $store -name $FolderName
        if ($target) { break }
    }
    if (-not $target) {
        Write-Error "Folder not found: $FolderName"
        exit 2
    }

    $items = $target.Items
    if ($UnreadOnly) {
        # Native Outlook index filter — not a manual walk-and-check, which
        # would be slow against a folder with a real backlog. Still sorted
        # newest-first so a capped MaxItems favors the most recent unread
        # mail first, same convention as the normal scan.
        $items = $items.Restrict("[Unread] = true")
    }
    $items.Sort("[ReceivedTime]", $true)  # newest first

    $count = 0
    foreach ($item in $items) {
        if ($item.Class -ne 43) { continue }  # olMail only (43) - skip meeting/other item classes

        $receivedEpoch = To-Epoch $item.ReceivedTime
        if (-not $UnreadOnly -and $receivedEpoch -le $SinceEpoch) { break }  # sorted newest-first: everything after this is older (cursor mode only — the sweep has no cursor boundary to respect)

        $senderSmtp = Resolve-SenderSmtp $item

        # Best-effort participant list: sender + To/CC display names, semicolon-joined by Outlook.
        $participants = @($senderSmtp)
        if ($item.To) { $participants += ($item.To -split ';' | ForEach-Object { $_.Trim() }) }
        if ($item.CC) { $participants += ($item.CC -split ';' | ForEach-Object { $_.Trim() }) }
        $participants = $participants | Where-Object { $_ -and $_.Trim().Length -gt 0 } | Select-Object -Unique

        $attResult = Save-RealAttachments -item $item -stagingDir $StagingDir

        $obj = [ordered]@{
            conversation_id        = $item.ConversationID
            entry_id               = $item.EntryID
            subject                = $item.Subject
            sender                 = $senderSmtp
            sender_name            = $item.SenderName
            participants           = $participants
            received_epoch         = $receivedEpoch
            body_preview           = $item.Body.Substring(0, [Math]::Min(500, $item.Body.Length))
            attachments            = $attResult.saved
            attachments_staged_dir = $attResult.dir
        }
        $obj | ConvertTo-Json -Compress -Depth 4
        $count++
        if ($count -ge $MaxItems) { break }
    }
} catch {
    Write-Error "ERROR: $($_.Exception.Message)"
    exit 1
}
