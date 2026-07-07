@echo off
REM phorg UI launcher — starts the friendly web interface and opens the browser.
setlocal
set "HERE=%~dp0"
python "%HERE%phorg" ui %*
endlocal
