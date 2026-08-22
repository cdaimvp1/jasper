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
set "JASPER_MCP_HOST=127.0.0.1"
set "JASPER_MCP_PORT=8701"
"C:\Users\lane_marc@lilly.com\Symphony\venv\Scripts\python.exe" jasper_mcp_server.py > "C:\Users\lane_marc@lilly.com\Symphony\body\data\jasper_mcp_server_startup.log" 2>&1
