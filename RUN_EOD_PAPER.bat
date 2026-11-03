@echo off
rem Runs the end-of-day paper test on this PC. Needs DATABENTO_API_KEY set (setx DATABENTO_API_KEY "db-...") 
cd /d "%~dp0"
if not exist logs mkdir logs
for /f %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyy-MM-dd"') do set TODAY=%%i
python mnq_eod_paper.py --source databento --workdir "%~dp0mnq_eod_live" >> "logs\eod_%TODAY%.log" 2>&1
