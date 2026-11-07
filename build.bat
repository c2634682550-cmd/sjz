@echo off
setlocal
cd /d "%~dp0"

where uv >nul 2>nul
if errorlevel 1 goto nouv

echo [0/7] Checking processes that hold dist / install dir...
powershell -NoProfile -ExecutionPolicy Bypass -File tools\check_locks.ps1
if errorlevel 2 (
  echo   -- build can continue, but overwrite/uninstall may fail
  echo   -- run the taskkill commands listed above first
)

echo [1/7] Syncing dependencies...
if "%SJZ_SKIP_SYNC%"=="1" (echo   skipped: SJZ_SKIP_SYNC=1) else (uv sync --group dev)
if errorlevel 1 goto nodeps

echo [2/7] Version sync (single source: core\version.py)...
uv run python tools\version_sync.py --write
if errorlevel 1 goto nover
if not exist "build\version_info.txt" goto nover

echo [3/7] Packaging audit (spec diff must be empty)...
uv run python tools\audit_packaging.py --json build\pack_audit.json
if errorlevel 1 goto noaudit

echo [4/7] Secret gate (no private keys in the tree before packaging)...
uv run python tools\scan_secrets.py --all
if errorlevel 1 goto nosecrets

echo [5/7] Running tests...
if "%SJZ_SKIP_TESTS%"=="1" (echo   skipped: SJZ_SKIP_TESTS=1) else (uv run pytest tests/ -q)
if errorlevel 1 goto notests

echo [5b/7] Driver assets gate (Interception installer must be complete)...
uv run python tools\check_driver_assets.py
if errorlevel 1 goto nodriver

echo [6/7] Building onedir (default delivery)...
uv run pyinstaller sjz.spec --noconfirm
if errorlevel 1 goto nobuild

echo [6b] Cleaning runtime leftovers from dist (避免把本机 config/台账 打进安装包)...
for %%f in (config.json config.json.good config.json.lock desktop.log) do (
  if exist "dist\SJZ-Control\%%f" del /q "dist\SJZ-Control\%%f"
)
if exist "dist\SJZ-Control\data" rmdir /s /q "dist\SJZ-Control\data"
if exist "dist\SJZ-Control\templates" rmdir /s /q "dist\SJZ-Control\templates"
if exist "dist\SJZ-Control\logs" rmdir /s /q "dist\SJZ-Control\logs"

echo [6c] Deploying .NET launcher into onedir (launcher\ + launcher.config.json)...
if exist "launcher\SjzLauncher.exe" (
  if not exist "dist\SJZ-Control\launcher" mkdir "dist\SJZ-Control\launcher"
  copy /y "launcher\SjzLauncher.exe" "dist\SJZ-Control\launcher\SjzLauncher.exe" >nul
  copy /y "launcher\launcher.config.json" "dist\SJZ-Control\launcher.config.json" >nul
  echo   launcher deployed
) else (
  echo   !! launcher\SjzLauncher.exe missing - installer will fall back to SJZ-Control.exe
  echo   !! build it first: powershell -File launcher\build.ps1
)

echo [7/7] Building onefile (optional single-exe)...
uv run pyinstaller sjz-onefile.spec --noconfirm --distpath dist-onefile
if errorlevel 1 goto nobuild

set "ISCC_EXE="
where iscc >nul 2>nul
if not errorlevel 1 set "ISCC_EXE=iscc"
if not defined ISCC_EXE if exist "%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe" set "ISCC_EXE=%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe"
if not defined ISCC_EXE if exist "%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe" set "ISCC_EXE=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if not defined ISCC_EXE (
  echo.
  echo Inno Setup not found - skipped installer. Install Inno Setup 6 and run:
  echo   iscc installer\sjz.iss
) else (
  echo [installer] Building setup with "%ISCC_EXE%"...
  "%ISCC_EXE%" installer\sjz.iss
  if errorlevel 1 goto nobuild
)

:done
echo.
echo Done.
echo   onedir   : dist\SJZ-Control\SJZ-Control.exe  (ship the whole folder)
echo   onefile  : dist-onefile\SJZ-Control.exe
echo   setup    : dist-installer\SJZ-Control-Setup-*.exe
echo Self-check: "dist\SJZ-Control\SJZ-Control.exe" --doctor
goto end

:nover
echo.
echo Version sync failed. Check core\version.py.
goto end

:noaudit
echo.
echo Packaging audit failed: spec does not collect everything the code needs.
echo See build\pack_audit.json
goto end

:notests
echo.
echo Tests failed. Not building a broken exe.
goto end

:nosecrets
echo.
echo Secret gate failed: the working tree contains private key material.
echo Move the key outside the repo (%LOCALAPPDATA%\SJZ-License-Keys\), then rerun.
echo See _license_keys.md section 7.
goto end

:nodriver
echo.
echo Driver assets gate failed: Interception installer missing or incomplete.
echo Expected tools\interception\install-interception.exe (official v1.0.1,
echo SHA256 E137863A79DA797F08E7A137280FF2A123809044A888FD75CE9C973198915ABE)
echo with embedded DRIVER/KBDNT + DRIVER/MOUNT resources.
echo See docs\driver-install.md
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
if not "%SJZ_NO_PAUSE%"=="1" pause
