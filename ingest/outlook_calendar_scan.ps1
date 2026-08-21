<#
outlook_calendar_scan.ps1 - deterministic Outlook calendar scan over COM. No LLM.

WHY THIS EXISTS (task #413). Calendar capture previously ran through the
relay - an LLM session asked to fetch events and write them to a drop file.
Measured 2026-08-20, that path silently lost data: one archived file read
{"events_catchup_count":25,"events_lookahead_count":25,"note":"Event details
truncated in this sample. Full implementation would include all 25 events
from each window."} - a DESCRIPTION of 50 events instead of the events, and
the calendar cursor advanced past them anyway. An LLM cannot be a reliable
data pipe.

Microsoft Graph would be the obvious fix; Lilly does not issue Graph
credentials, so it is permanently unavailable. But the SAME MAPI namespace
outlook_scan.ps1 already uses for mail also exposes the calendar via
GetDefaultFolder(9) - probed live: 2,492 items available versus 59 then
ingested. Deterministic, local, credential-free, and an extension of a
mechanism already in production rather than a new access path.

OUTPUT: a single JSON object on stdout in the SAME shape the relay was
supposed to emit - {"source":"calendar","events":[...]} with each event
carrying id/subject/organizer/attendees/start/end/isOrganizer/location/
isCancelled/showAs/importance/recurrence. That shape is deliberate: it means
ingest/normalize.py's already-tested calendar parsing, its recurring-series
thread_key logic, and the drop-file guard added in commit 2d3ba0a all apply
unchanged. Only the unreliable transcription step is replaced.

Read-only: opens Outlook, reads the default calendar, writes nothing back.

Usage:
    powershell -File outlook_calendar_scan.ps1 [-DaysBack 120] [-DaysForward 60] [-MaxItems 1000]
#>
param(
    [int]$DaysBack = 120,
    [int]$DaysForward = 60,
    [int]$MaxItems = 1000
)

$ErrorActionPreference = "Stop"

# Same line outlook_scan.ps1 carries (its line 50), and for the same reason:
# without it PowerShell emits cp1252, and the Python side reads stdout with
# encoding="utf-8", so any non-ASCII character in a subject or attendee name
# raises UnicodeDecodeError and loses the whole scan. Hit this immediately on
# the first live run against real calendar data.
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

try {
    $outlook = New-Object -ComObject Outlook.Application
    $ns = $outlook.GetNamespace("MAPI")
    $cal = $ns.GetDefaultFolder(9)   # olFolderCalendar

    $items = $cal.Items
    # IncludeRecurrences must be set BEFORE Sort, and Sort must be ascending on
    # [Start], or Outlook silently returns only the master appointment for a
    # recurring series instead of the individual occurrences.
    $items.IncludeRecurrences = $true
    $items.Sort("[Start]")

    $windowStart = (Get-Date).AddDays(-$DaysBack)
    $windowEnd   = (Get-Date).AddDays($DaysForward)
    $filter = "[Start] >= '" + $windowStart.ToString("g") + "' AND [Start] <= '" + $windowEnd.ToString("g") + "'"
    $restricted = $items.Restrict($filter)

    $events = @()
    $count = 0
    foreach ($appt in $restricted) {
        if ($count -ge $MaxItems) { break }
        try {
            # Recipients -> plain address list, matching what the relay shape
            # used (attendees is a flat list of strings; attendees_detailed
            # carries the richer form when available - see normalize.py E7).
            $attendees = @()
            $attendeesDetailed = @()
            foreach ($r in $appt.Recipients) {
                $addr = $null
                try { $addr = $r.Address } catch { $addr = $null }
                if (-not $addr) { $addr = $r.Name }
                if ($addr) {
                    $attendees += $addr
                    $attendeesDetailed += @{ name = $r.Name; address = $addr }
                }
            }

            $isRecurring = $false
            try { $isRecurring = [bool]$appt.IsRecurring } catch { $isRecurring = $false }

            $ev = @{
                # EntryID is stable per occurrence and is what normalize.py uses
                # as the raw stable_key; GlobalAppointmentID ties occurrences of
                # one series together and feeds _calendar_series_key.
                id                   = $appt.EntryID
                series_id            = $(try { $appt.GlobalAppointmentID } catch { $null })
                subject              = $appt.Subject
                organizer            = $appt.Organizer
                attendees            = $attendees
                attendees_detailed   = $attendeesDetailed
                start                = @{ dateTime = $appt.Start.ToString("yyyy-MM-ddTHH:mm:ss"); timeZone = "local" }
                end                  = @{ dateTime = $appt.End.ToString("yyyy-MM-ddTHH:mm:ss"); timeZone = "local" }
                location             = $appt.Location
                isOrganizer          = ($appt.Organizer -eq $ns.CurrentUser.Name)
                isCancelled          = $(try { [bool]$appt.MeetingStatus -band 5 } catch { $false })
                showAs               = $(try { [string]$appt.BusyStatus } catch { $null })
                importance           = $(try { [string]$appt.Importance } catch { $null })
                # normalize.py treats a non-null `recurrence` as is_recurring.
                recurrence           = $(if ($isRecurring) { @{ isRecurring = $true } } else { $null })
                body_preview         = $(try { if ($appt.Body) { $appt.Body.Substring(0, [Math]::Min(500, $appt.Body.Length)) } else { $null } } catch { $null })
            }
            $events += $ev
            $count++
        } catch {
            # One bad appointment must never abort the scan - same
            # never-let-one-failure-block-the-rest discipline as the mail path.
            Write-Warning "skipping one appointment: $($_.Exception.Message)"
            continue
        }
    }

    # events_count is emitted deliberately: the drop-file guard cross-checks any
    # *_count against the number of items actually parsed, so if this scanner
    # ever produces a count without the payload the file fails loudly instead
    # of being archived as a success.
    $payload = @{
        source       = "calendar"
        events       = $events
        events_count = $events.Count
        window_start = $windowStart.ToString("yyyy-MM-ddTHH:mm:ss")
        window_end   = $windowEnd.ToString("yyyy-MM-ddTHH:mm:ss")
    }
    $payload | ConvertTo-Json -Compress -Depth 6
} catch {
    Write-Error "ERROR: $($_.Exception.Message)"
    exit 1
}
