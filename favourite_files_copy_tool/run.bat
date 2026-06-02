@echo off
cd /d "%~dp0"

rem Optional: run.bat "E:\Photos\Base"  -- Base is NOT required to open GUI

where py >nul 2>&1 && goto use_py
where python >nul 2>&1 && goto use_python

echo [ERROR] Python not found. Install Python 3 and add to PATH.
pause
exit /b 1

:use_py
py -3 -c "import PIL, cv2" 2>nul
if errorlevel 1 (
    echo Installing dependencies from requirements.txt ...
    py -3 -m pip install -r requirements.txt
    if errorlevel 1 (
        echo [ERROR] pip install failed. Run: py -3 -m pip install -r requirements.txt
        pause
        exit /b 1
    )
)
py -3 favourite_files_app.py %*
if errorlevel 1 pause
exit /b 0

:use_python
python -c "import PIL, cv2" 2>nul
if errorlevel 1 (
    echo Installing dependencies from requirements.txt ...
    python -m pip install -r requirements.txt
    if errorlevel 1 (
        echo [ERROR] pip install failed. Run: python -m pip install -r requirements.txt
        pause
        exit /b 1
    )
)
python favourite_files_app.py %*
if errorlevel 1 pause
exit /b 0
