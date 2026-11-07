@echo off
setlocal
cd /d "%~dp0"
title SJZ 抢购控制台

rem ============================================================
rem 解释器探测顺序（审查 R09-06：.venv 里的 pyvenv.cfg 写死了开发机
rem 路径，整个目录拷到别的机器后 python.exe 其实起不来，双击毫无反应）：
rem   1. .venv —— 必须**真的能跑**（解释器自检 + 核心依赖导入）才用
rem   2. uv       —— 缺失/损坏的 .venv 会被 uv 重建并装依赖
rem   3. 系统 python —— 只做可用性探测，版本 < 3.10 直接判不可用
rem   4. 全都不可用 → 给出可读提示并暂停（别一闪而过）
rem ============================================================

set "VENV_PY=.venv\Scripts\python.exe"
set "VENV_PYW=.venv\Scripts\pythonw.exe"

if exist "%VENV_PY%" (
    "%VENV_PY%" -c "import sys,cv2,mss,numpy; assert sys.prefix" >nul 2>nul
    if not errorlevel 1 goto venv_ok
    echo [探测] .venv 存在但不可用（通常是 pyvenv.cfg 指向了开发机路径），跳过。
)

where uv >nul 2>nul
if not errorlevel 1 goto uvrun

where python >nul 2>nul
if not errorlevel 1 goto probe_syspy

echo.
echo 没找到可用的 Python 环境：
echo   1) 装了 uv：powershell -c "irm https://astral.sh/uv/install.ps1 ^| iex"
echo      然后双击本文件（会自动 uv sync 装依赖）
echo   2) 或装 Python 3.10+，重开本文件
echo   3) 本机已有 .venv 但拷过来用不了：删掉 .venv 再双击（会走 uv 重建）
echo.
echo 详细原因见 desktop.log（如果曾经成功启动过）。
pause
goto end

:venv_ok
rem pythonw 才不会留黑框；万一缺 pythonw.exe 就用 python.exe（至少能跑）
if exist "%VENV_PYW%" (
    start "" "%VENV_PYW%" desktop.py
) else (
    echo [提示] .venv 缺 pythonw.exe，改用 python.exe（会保留控制台窗口）
    "%VENV_PY%" desktop.py
    if errorlevel 1 pause
)
goto end

:uvrun
echo [1/2] uv sync（.venv 损坏时 uv 会重建，首次需要联网下载依赖）...
uv sync
if errorlevel 1 goto nodeps
echo [2/2] 启动...
uv run python desktop.py
if errorlevel 1 pause
goto end

:probe_syspy
python -c "import sys; assert sys.version_info[:2] >= (3,10)" >nul 2>nul
if errorlevel 1 goto badver
python -c "import cv2,mss,numpy" >nul 2>nul
if errorlevel 1 goto nodeps
python desktop.py
if errorlevel 1 pause
goto end

:badver
echo.
echo 系统 Python 版本低于 3.10（本项目要求 3.10+）。
echo   装新版本后重开本文件，或用 uv：powershell -c "irm https://astral.sh/uv/install.ps1 ^| iex"
echo.
pause
goto end

:nodeps
echo.
echo 依赖安装/导入失败。检查：
echo   1) 网络是否可用（首次要下载 opencv / numpy / onnxruntime）
echo   2) 是否被杀毒拦住写入 .venv
echo   3) 删掉 .venv 后重开本文件，让 uv 重建
echo.
pause

:end
endlocal
