# Switch Symphony's next-launched Claude Code session(s) back to your primary
# (main) Claude Code account. Takes effect on the NEXT session start for each worker.
'primary' | Set-Content -LiteralPath 'C:\Users\lane_marc@lilly.com\Symphony\body\config\llm_active_profile.txt' -NoNewline
Add-Content -LiteralPath 'C:\Users\lane_marc@lilly.com\Symphony\body\config\llm_active_profile.txt' -Value ''
Write-Host "Switched back to primary. Restart Claude Code sessions to pick it up."
