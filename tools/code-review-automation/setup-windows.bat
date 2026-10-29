@echo off
rem Double-click to run the setup wizard.
cd /d "%~dp0"
where py >nul 2>nul
if errorlevel 1 (
  echo Python 3 is needed. Install it with: winget install Python.Python.3.12
  echo or from https://www.python.org/downloads/ then run this again.
  pause
  exit /b 1
)
py -3 code_review_automation.py setup
pause
