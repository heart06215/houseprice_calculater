@echo off
chcp 65001 >nul 2>&1
setlocal
title 70城房价信息平台 - 本地服务

echo ============================================================
echo   70城房价信息平台 - 本地服务
echo   访问地址: http://127.0.0.1:8765
echo ============================================================
echo.

cd /d "%~dp0"

:: 依次尝试: 项目虚拟环境 > 系统 python > py 启动器,每个候选先冒烟测试确认可运行
set "PYTHON="
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" -c "import sys" >nul 2>nul && set "PYTHON=.venv\Scripts\python.exe"
)
if not defined PYTHON (
    python -c "import sys" >nul 2>nul && set "PYTHON=python"
)
if not defined PYTHON (
    py -3 -c "import sys" >nul 2>nul && set "PYTHON=py -3"
)
if not defined PYTHON (
    echo [错误] 未找到可用的 Python,请先安装 Python 3
    echo.
    pause
    exit /b 1
)

echo [信息] 使用 Python: %PYTHON%
echo [信息] 正在启动本地服务,浏览器将自动打开...
echo [提示] 关闭本窗口或按 Ctrl+C 可停止服务
echo.

:: 不加引号是刻意的: 路径均为无空格相对路径,且 "py -3" 需保持拆分
%PYTHON% "data_processing\launch_frontend.py"

echo.
echo [提示] 服务已停止
pause
