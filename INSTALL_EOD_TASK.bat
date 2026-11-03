@echo off
rem Creates a scheduled task: every weekday at 16:45 (computer clock must be Eastern time). If the PC was off, the next run catches up.
schtasks /create /tn "MNQ EOD Paper Test" /tr "\"%~dp0RUN_EOD_PAPER.bat\"" /sc weekly /d MON,TUE,WED,THU,FRI /st 16:45 /f
pause
