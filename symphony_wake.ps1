# Symphony hands-off wake (Windows) - GENERATED. Usage: .\symphony_wake.ps1 <worker> [claude args]
param([Parameter(Mandatory=$true)][string]$Worker)
$_r = (& "C:\Users\lane_marc@lilly.com\Symphony\venv\Scripts\python.exe" -c "import json,os,sys;w=sys.argv[1];th=sys.argv[2];p=os.path.join(th,'config','symphony_identity.json');r=(json.load(open(p)).get('roles',{}) if os.path.exists(p) else {});print(w if w in r else next((k for k,v in r.items() if isinstance(v,dict) and str(v.get('display_name','')).lower()==w.lower()),w))" $Worker "C:\Users\lane_marc@lilly.com\Symphony\body" 2>$null); if ($_r) { $Worker = $_r }
$env:SYMPHONY_WORKER = $Worker
. 'C:\Users\lane_marc@lilly.com\Symphony\body\symphony_env.ps1'
try { & "C:\Users\lane_marc@lilly.com\Symphony\venv\Scripts\python.exe" -c "import sys,os; r=os.environ.get('TEAM_SCRIPTS_ROOT',''); w=os.environ.get('SYMPHONY_WORKER',''); b=os.environ.get('COHORT_BASE',''); _s=(sys.path.insert(0,r), __import__('poller_autostart').ensure_poller_alive(w,'cohort_f9_poller.py '+w+((' '+b) if b else '')))[1] if (r and w) else 'SKIPPED (TEAM_SCRIPTS_ROOT or SYMPHONY_WORKER unset)'; print('[wake-arm]', _s)" 2>$null } catch {}
Set-Location 'C:\Users\lane_marc@lilly.com\Symphony\body'
$_slotSettings = 'C:\Users\lane_marc@lilly.com\Symphony\body\workers\' + $Worker + '\settings.json'
$rest = $args
if (Test-Path $_slotSettings) { $rest = @('--settings', $_slotSettings) + $rest }
$_claudeCmd = (Get-Command claude -ErrorAction SilentlyContinue).Source
if (-not $_claudeCmd) {
    $_cand = Join-Path $env:USERPROFILE '.local\bin\claude.exe'
    if (Test-Path $_cand) { $_claudeCmd = $_cand }
}
if (-not $_claudeCmd) { $_claudeCmd = 'claude' }   # last resort - matches old behavior, will error clearly if truly absent
$_gitCmd = (Get-Command git -ErrorAction SilentlyContinue).Source
if ($_gitCmd) {
    $_bashCand = Join-Path (Split-Path (Split-Path $_gitCmd -Parent) -Parent) 'bin\bash.exe'
    if (Test-Path $_bashCand) { $env:CLAUDE_CODE_GIT_BASH_PATH = $_bashCand }
}
if (-not $env:CLAUDE_CODE_GIT_BASH_PATH) {
    $_bashCand2 = 'C:\Program Files\Git\bin\bash.exe'
    if (Test-Path $_bashCand2) { $env:CLAUDE_CODE_GIT_BASH_PATH = $_bashCand2 }
}
& $_claudeCmd @rest
