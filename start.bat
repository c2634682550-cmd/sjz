@echo off
setlocal
cd /d "%~dp0"
title SJZ 抢购控制台

rem 优先用项目自带的 .venv：双击直接出窗口，不留黑框。
rem 桌面版出问题会写 desktop.log，致命错误还会弹消息框（见 desktop.py 末尾）。
if exist ".venv\Scripts\pythonw.exe" goto venv

rem 没有 .venv 就退回 uv（首次会 sync 依赖）
where uv >nul 2>nul
if not errorlevel 1 goto uvrun

rem 再退回系统 python
where python >nul 2>nul
if not errorlevel 1 goto syspy

echo.
echo 没找到可用的 Python 环境。
echo   装了 uv 的话：powershell -c "irm https://astral.sh/uv/install.ps1 ^| iex"
echo   或者装 Python 3.11+ 后重开这个文件。
echo.
pause
goto end

:venv
start "" ".venv\Scripts\pythonw.exe" desktop.py
goto end

:uvrun
echo [1/2] Syncing dependencies with uv...
uv sync
if errorlevel 1 goto nodeps
echo [2/2] Ready.
uv run python desktop.py
goto end

:syspy
python desktop.py
goto end

:nodeps
echo.
echo 依赖安装失败，检查网络后重试。
echo.
pause

:end
endlocal
