# outlook_draft_compose.ps1 - creates a REAL Outlook draft new-mail item
# addressed to the given recipients, and displays it for review - task #35
# (replaces the interim client-only mailto: link the cockpit UI used while
# this wasn't built yet). Uses Outlook's own CreateItem(0) (olMailItem) -
# never calls Send(). Display() puts it on screen exactly like a person
# clicking New Email themselves; nothing here transmits anything.
#
# Usage:
#   powershell -File outlook_draft_compose.ps1 -To "a@x.com;b@y.com" -Subject "..."
param(
    [Parameter(Mandatory=$true)][string]$To,
    [Parameter(Mandatory=$true)][string]$Subject
)

try {
    $outlook = New-Object -ComObject Outlook.Application
    $mail = $outlook.CreateItem(0)
    $mail.To = $To
    $mail.Subject = $Subject
    $mail.Display()
    Write-Output '{"ok":true}'
} catch {
    Write-Error "ERROR: $($_.Exception.Message)"
    exit 1
}
