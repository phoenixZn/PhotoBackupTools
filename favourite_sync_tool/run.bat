@echo off
setlocal

cd /d "%~dp0"

where py >nul 2>nul
if %errorlevel%==0 (
    py -3 run_favourite_sync_gui.py %*
) else (
    python run_favourite_sync_gui.py %*
)

endlocal