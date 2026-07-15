@echo off
REM ===========================================================================
REM  Build phorg into a single standalone Windows .exe (no Python needed to run)
REM  Requires: Python + PyInstaller  (this script installs PyInstaller if missing)
REM  Output:   dist\phorg.exe
REM
REM  CODE SIGNING (fixes the "Smart App Control / unverified publisher" block):
REM    Windows blocks unsigned apps. To sign automatically, set these before
REM    running this script (a code-signing certificate from a trusted CA is
REM    required - a self-signed cert will NOT satisfy Smart App Control):
REM        set PHORG_PFX=C:\path\to\your-cert.pfx
REM        set PHORG_PFX_PASSWORD=your-pfx-password
REM    Optional (defaults shown):
REM        set PHORG_TIMESTAMP_URL=http://timestamp.digicert.com
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
REM Build from the spec file so output is identical and UPX-free
REM (UPX compression is a common antivirus / SmartScreen false-positive trigger).
python -m PyInstaller --noconfirm --clean phorg.spec

if errorlevel 1 (
    echo.
    echo   [!] Build failed.
    pause
    exit /b 1
)

REM -------------------------------------------------------------------------
REM  Optional: code-sign the exe so Windows can verify the publisher.
REM -------------------------------------------------------------------------
if defined PHORG_PFX (
    if not defined PHORG_TIMESTAMP_URL set "PHORG_TIMESTAMP_URL=http://timestamp.digicert.com"
    echo.
    echo   Signing dist\phorg.exe ...
    where signtool >nul 2>&1
    if errorlevel 1 (
        echo   [!] signtool not found. Install the Windows SDK ^(App Certification Kit^),
        echo       then re-run, or sign manually. Skipping signing.
    ) else (
        signtool sign /fd SHA256 /f "%PHORG_PFX%" /p "%PHORG_PFX_PASSWORD%" ^
            /tr "%PHORG_TIMESTAMP_URL%" /td SHA256 "dist\phorg.exe"
        if errorlevel 1 (
            echo   [!] Signing failed. The exe was still built, but unsigned.
        ) else (
            echo   Signed. Verifying...
            signtool verify /pa "dist\phorg.exe"
        )
    )
) else (
    echo.
    echo   [i] PHORG_PFX not set - exe is UNSIGNED.
    echo       Unsigned apps are blocked by Smart App Control / SmartScreen.
    echo       See the header of this file to enable automatic signing.
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
