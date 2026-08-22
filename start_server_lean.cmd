@echo off
cd /d "C:\Users\lane_marc@lilly.com\Symphony\body"
set "SYMPHONY_INSTALL_ROOT=C:\Users\lane_marc@lilly.com\Symphony"
set "TEAM_HOME=C:\Users\lane_marc@lilly.com\Symphony\body"
set "TEAM_CONFIG_DIR=C:\Users\lane_marc@lilly.com\Symphony\body\config"
set "TEAM_WORKSPACE_ROOT=C:\Users\lane_marc@lilly.com\Symphony\workspace"
set "TEAM_DATA_DIR=C:\Users\lane_marc@lilly.com\Symphony\new_cohort\data"
set "TEAM_SCRIPTS_ROOT=C:\Users\lane_marc@lilly.com\Symphony\body/setup"
set "TEAM_PORT=8700"
set "SYMPHONY_PORT=8700"
set "COHORT_BASE=http://localhost:8700"
"C:\Users\lane_marc@lilly.com\Symphony\venv\Scripts\python.exe" -m uvicorn server_lean:app --host 127.0.0.1 --port 8700 > "C:\Users\lane_marc@lilly.com\Symphony\body\data\server_lean_startup.log" 2>&1
