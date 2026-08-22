# Switch Symphony's next-launched Claude Code session(s) to the Lilly Code
# fallback profile. Takes effect on the NEXT session start for each worker —
# restart Claude Code (or let a worker's next wake pick it up) after running this.
'fallback' | Set-Content -LiteralPath 'C:\Users\lane_marc@lilly.com\Symphony\body\config\llm_active_profile.txt' -NoNewline
Add-Content -LiteralPath 'C:\Users\lane_marc@lilly.com\Symphony\body\config\llm_active_profile.txt' -Value ''
Write-Host "Switched to fallback (Lilly Code). Restart Claude Code sessions to pick it up."
Write-Host "If this warns about REPLACE_ME placeholders on next wake, fill in body\config\llm_profile.fallback.ps1 first."
