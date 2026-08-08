# outlook_draft_compose.ps1 - creates a REAL Outlook draft new-mail item
# addressed to the given recipients, and displays it for review - task #35
# (replaces the interim client-only mailto: link the cockpit UI used while
# this wasn't built yet). Uses Outlook's own CreateItem(0) (olMailItem) -
# never calls Send(). Display() puts it on screen exactly like a person
# clicking New Email themselves; nothing here transmits anything.
#
# Body/AttachmentPaths (task #35 follow-on, 2026-08-08): the real path to
# "share this output with stakeholders and ask them to review" - Marc's
# own question was whether this needs new M365/Graph write permissions
# (SharePoint sharing); it doesn't. This is the same local COM automation
# already used for draft_reply/draft_forward/mark_read, just given a body
# and a real file to attach. AttachmentPaths is semicolon-separated (same
# convention as -To) since a review request may carry more than one file.
# A missing attachment path is reported back, never silently dropped -
# same "always report reality" discipline as body_capture_failures
# elsewhere in this codebase - so a caller never believes a doc was
# attached when it wasn't.
#
# Usage:
#   powershell -File outlook_draft_compose.ps1 -To "a@x.com;b@y.com" -Subject "..." `
#       -Body "Please review and let me know your thoughts." `
#       -AttachmentPaths "C:\path\to\redline.docx"
param(
    [Parameter(Mandatory=$true)][string]$To,
    [Parameter(Mandatory=$true)][string]$Subject,
    [string]$Body = "",
    [string]$AttachmentPaths = ""
)

try {
    $outlook = New-Object -ComObject Outlook.Application
    $mail = $outlook.CreateItem(0)
    $mail.To = $To
    $mail.Subject = $Subject
    if ($Body) { $mail.Body = $Body }

    $attached = @()
    $missing = @()
    if ($AttachmentPaths) {
        foreach ($path in ($AttachmentPaths -split ';' | Where-Object { $_ })) {
            if (Test-Path -LiteralPath $path -PathType Leaf) {
                $mail.Attachments.Add($path) | Out-Null
                $attached += $path
            } else {
                $missing += $path
            }
        }
    }

    $mail.Display()
    $result = @{ ok = $true; attached = $attached; missing_attachments = $missing }
    Write-Output ($result | ConvertTo-Json -Compress)
} catch {
    Write-Error "ERROR: $($_.Exception.Message)"
    exit 1
}
