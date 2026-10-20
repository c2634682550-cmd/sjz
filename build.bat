@echo off
setlocal
cd /d "%~dp0"

where uv >nul 2>nul
if errorlevel 1 goto nouv

echo [1/3] Syncing dependencies...
uv sync --group dev
if errorlevel 1 goto nodeps

echo [2/3] Running tests...
uv run pytest tests/ -q
if errorlevel 1 goto notests

echo [3/3] Building the exe...
uv run pyinstaller sjz.spec --noconfirm
if errorlevel 1 goto nobuild

echo.
echo Done. Exe is at dist\SJZ-Control.exe
echo Self-check:  "dist\SJZ-Control.exe" --doctor
goto end

:notests
echo.
echo Tests failed. Not building a broken exe.
goto end

:nobuild
echo.
echo Build failed. See docs\build-desktop.md
goto end

:nouv
echo.
echo uv not found. Install it first:
echo   powershell -c "irm https://astral.sh/uv/install.ps1 ^| iex"
goto end

:nodeps
echo.
echo Dependency sync failed. Check your network and retry.
goto end

:end
echo.
pause
