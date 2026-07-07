@echo off
REM ===========================================================================
REM  Build phorg into a single standalone Windows .exe (no Python needed to run)
REM  Requires: Python + PyInstaller  (this script installs PyInstaller if missing)
REM  Output:   dist\phorg.exe
REM ===========================================================================
setlocal
cd /d "%~dp0"

echo.
echo   Checking for PyInstaller...
python -m PyInstaller --version >nul 2>&1
if errorlevel 1 (
    echo   Installing PyInstaller...
    python -m pip install --upgrade pyinstaller
    if errorlevel 1 (
        echo.
        echo   [!] Could not install PyInstaller. Check your internet connection.
        pause
        exit /b 1
    )
)

echo.
echo   Building phorg.exe ...
python -m PyInstaller --noconfirm --clean --onefile ^
    --name phorg ^
    --add-data "phorg\ui.html;phorg" ^
    --add-data "phorg\report_template.html;phorg" ^
    --add-data "phorg\haarcascade_frontalface_default.xml;phorg" ^
    --collect-data cv2 ^
    run_phorg.py

if errorlevel 1 (
    echo.
    echo   [!] Build failed.
    pause
    exit /b 1
)

echo.
echo   ============================================================
echo    Done!  Your shareable app is here:
echo        dist\phorg.exe
echo.
echo    Send that single file to your friends. They just
echo    double-click it - no Python or install required.
echo   ============================================================
echo.
pause
endlocal
