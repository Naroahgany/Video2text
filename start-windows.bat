@echo off
setlocal

set "PROJECT_ROOT=%~dp0"
set "SCRIPT=%PROJECT_ROOT%scripts\start-local.ps1"

if not exist "%SCRIPT%" (
  echo [ERROR] Missing startup helper: %SCRIPT%
  echo Please re-download the project zip or restore the scripts directory.
  pause
  exit /b 1
)

powershell -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT%"
set "EXIT_CODE=%ERRORLEVEL%"

if not "%EXIT_CODE%"=="0" (
  echo.
  echo Startup failed. Please read the message above and try again.
  pause
)

exit /b %EXIT_CODE%
