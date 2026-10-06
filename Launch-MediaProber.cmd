@echo off
setlocal DisableDelayedExpansion
set "python_bin=%~dp0.venv\Scripts\python.exe"
if not exist "%python_bin%" (
  echo The local Python environment is missing. See README.md for Windows setup.
  pause
  exit /b 1
)
"%python_bin%" "%~dp0MediaProber.py" %*
if errorlevel 1 (
  pause
  exit /b 1
)
