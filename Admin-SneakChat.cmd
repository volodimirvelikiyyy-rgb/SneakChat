@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0Start-SneakChat.ps1" -View admin %*
if errorlevel 1 (
  echo SneakChat Admin could not open. See the error above.
  pause
  exit /b 1
)
