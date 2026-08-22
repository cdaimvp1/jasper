# Symphony LLM profile selector — hand-maintained, NOT generated.
# Dot-sourced from body/symphony_env.ps1 on every wake. Reads the active
# profile flag and sets (or clears) $env:ANTHROPIC_BASE_URL / ANTHROPIC_API_KEY
# accordingly, so symphony_sessionstart.ps1 can forward them into the Claude
# Code session exactly like every other Symphony env var.
#
# Profiles:
#   primary  (default) — no override; Claude Code uses your normal logged-in
#                         account, same as before this mechanism existed.
#   fallback            — sources llm_profile.fallback.ps1 (Lilly Code creds).
#
# Switch with body/setup/symphony_use_lilly_code.ps1 / symphony_use_primary.ps1.
# A switch only takes effect on the NEXT Claude Code session start (env is
# injected at wake, not live mid-session).

$flagFile = 'C:\Users\lane_marc@lilly.com\Symphony\body\config\llm_active_profile.txt'
$fallbackPs = 'C:\Users\lane_marc@lilly.com\Symphony\body\config\llm_profile.fallback.ps1'

$profile_ = 'primary'
if (Test-Path $flagFile) {
  $raw = (Get-Content -LiteralPath $flagFile -Raw -ErrorAction SilentlyContinue)
  if ($raw) { $profile_ = $raw.Trim().ToLowerInvariant() }
}

# Always start clean so a stale fallback value never leaks into a primary session.
Remove-Item Env:\ANTHROPIC_BASE_URL -ErrorAction SilentlyContinue
Remove-Item Env:\ANTHROPIC_API_KEY -ErrorAction SilentlyContinue

if ($profile_ -eq 'fallback') {
  if (-not (Test-Path $fallbackPs)) {
    Write-Warning "[llm-profile] fallback selected but $fallbackPs is missing — staying on primary."
  } else {
    . $fallbackPs
    if ($env:ANTHROPIC_API_KEY -eq 'REPLACE_ME' -or $env:ANTHROPIC_BASE_URL -eq 'REPLACE_ME') {
      Write-Warning "[llm-profile] fallback selected but llm_profile.fallback.ps1 still has REPLACE_ME placeholders — staying on primary. Fill in your Lilly Code credentials first."
      Remove-Item Env:\ANTHROPIC_BASE_URL -ErrorAction SilentlyContinue
      Remove-Item Env:\ANTHROPIC_API_KEY -ErrorAction SilentlyContinue
    } else {
      Write-Host "[llm-profile] active profile: fallback (Lilly Code)"
    }
  }
} else {
  Write-Host "[llm-profile] active profile: primary"
}
