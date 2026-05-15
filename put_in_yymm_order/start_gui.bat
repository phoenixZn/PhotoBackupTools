@echo off
setlocal

cd /d "%~dp0"

where py >nul 2>nul
if %errorlevel%==0 (
    py -3 gui_launcher.py %*
) else (
    python gui_launcher.py %*
)

endlocal
