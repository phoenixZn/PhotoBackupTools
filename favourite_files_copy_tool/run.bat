@echo off
cd /d "%~dp0"
where py >nul 2>&1
if %errorlevel%==0 (
    py -3 favourite_files_app.py %*
) else (
    python favourite_files_app.py %*
)
