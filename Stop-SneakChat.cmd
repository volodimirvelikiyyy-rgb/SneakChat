@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0Start-SneakChat.ps1" -Stop -NoBrowser %*
if errorlevel 1 (
  echo SneakChat could not stop. See the error above.
  pause
  exit /b 1
)
