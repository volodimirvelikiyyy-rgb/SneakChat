@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0Start-SneakChat.ps1" %*
if errorlevel 1 (
  echo SneakChat could not start. See the error above.
  pause
  exit /b 1
)
