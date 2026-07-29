# Symphony SessionStart hook (Windows) — AUTONOMOUS wake · GENERATED, do not hand-edit.
# Self-sources the body env (no manual dot-source), injects it into the CC session via
# CLAUDE_ENV_FILE, then auto-arms the F9 poller (bus-aware). Best-effort; never fails the wake.
$envPs = 'C:\Users\lane_marc@lilly.com\Symphony\body\symphony_env.ps1'
if (Test-Path $envPs) { . $envPs }
if ($env:CLAUDE_ENV_FILE) {
  foreach ($v in 'TEAM_WORKSPACE_ROOT','TEAM_HOME','SYMPHONY_INSTALL_ROOT','TEAM_SCRIPTS_ROOT','TEAM_DATA_DIR','SYMPHONY_SOUL_ROOT','TEAM_PORT','SYMPHONY_PORT','COHORT_BASE','TEAM_PID_FILE','SYMPHONY_BODY_SOURCE') {
    $val = [Environment]::GetEnvironmentVariable($v)
    if ($val) { Add-Content -LiteralPath $env:CLAUDE_ENV_FILE -Value "export $v=`"$val`"" }
  }
}
if ($env:SYMPHONY_WORKER -and $env:TEAM_SCRIPTS_ROOT) {
  & "C:\Users\lane_marc@lilly.com\Symphony\venv\Scripts\python.exe" -c "import sys,os; r=os.environ.get('TEAM_SCRIPTS_ROOT',''); w=os.environ.get('SYMPHONY_WORKER',''); b=os.environ.get('COHORT_BASE',''); _s=(sys.path.insert(0,r), __import__('poller_autostart').ensure_poller_alive(w,'cohort_f9_poller.py '+w+((' '+b) if b else '')))[1] if (r and w) else 'SKIPPED (TEAM_SCRIPTS_ROOT or SYMPHONY_WORKER unset)'; print('[wake-arm]', _s)" 2>$null
}
exit 0
