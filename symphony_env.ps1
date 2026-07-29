# Symphony machine env (dot-source before launch: . .\symphony_env.ps1)
$env:TEAM_WORKSPACE_ROOT="C:\Users\lane_marc@lilly.com\Symphony\workspace"
$env:TEAM_HOME="C:\Users\lane_marc@lilly.com\Symphony\body"
$env:SYMPHONY_INSTALL_ROOT="C:\Users\lane_marc@lilly.com\Symphony"
$env:TEAM_SCRIPTS_ROOT="C:\Users\lane_marc@lilly.com\Symphony\body/setup"
$env:SYMPHONY_BODY_SOURCE="None"
$env:TEAM_DATA_DIR="C:\Users\lane_marc@lilly.com\Symphony\new_cohort\data"
$env:TEAM_PID_FILE="C:\Users\lane_marc@lilly.com\Symphony\new_cohort\data/team_server.pid"
$env:SYMPHONY_SOUL_ROOT="soul_dev"
$env:TEAM_PORT="8700"
$env:SYMPHONY_PORT="8700"
$env:COHORT_BASE="http://localhost:8700"
Remove-Item Env:\SYMPHONY_EL_ROOT -ErrorAction SilentlyContinue
