@echo off
REM phorg launcher — runs the CLI from this folder regardless of cwd.
setlocal
set "HERE=%~dp0"
python "%HERE%phorg" %*
endlocal
