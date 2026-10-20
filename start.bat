@echo off
setlocal
cd /d "%~dp0"

where uv >nul 2>nul
if errorlevel 1 goto nouv

echo [1/2] Syncing dependencies with uv...
uv sync
if errorlevel 1 goto nodeps
echo [2/2] Ready.

uv run python run.py
goto end

:nouv
echo.
echo uv not found. Install it first:
echo   powershell -c "irm https://astral.sh/uv/install.ps1 ^| iex"
echo   (or: winget install --id=astral-sh.uv)
goto end

:nodeps
echo.
echo Dependency sync failed. Check your network and retry.
goto end

:end
echo.
pause
