@echo off
rem Copyright (C) 2026 Diderde
rem SPDX-License-Identifier: MIT
chcp 65001 >nul
setlocal
title TTS-Hub
cd /d "%~dp0"

echo [TTS-Hub] 项目根：%CD%

where python >nul 2>nul
if errorlevel 1 (
    echo [错误] 未找到 python，请先安装 Python 3.11+ 并加入 PATH。
    pause
    exit /b 1
)

if exist ".venv\Scripts\python.exe" (
    echo [TTS-Hub] 虚拟环境已就绪。
) else (
    echo [TTS-Hub] 首次运行：创建虚拟环境 .venv ...
    python -m venv .venv
    if errorlevel 1 goto :fail
)

if exist ".venv\Scripts\tts-hub.exe" (
    echo [TTS-Hub] 依赖已安装。
) else (
    echo [TTS-Hub] 安装依赖（含 HTTP 服务所需的可选组 [server]，首次约需一两分钟）...
    ".venv\Scripts\python.exe" -m pip install -e ".[server]"
    if errorlevel 1 goto :fail
)

if exist ".env" (
    echo [TTS-Hub] 已有 .env。
) else if exist ".env.example" (
    copy /y ".env.example" ".env" >nul
    echo [提示] 已从 .env.example 生成 .env：填入各厂商 API Key 后才能真实调用，不填也能启动服务与管理台。
) else (
    echo [提示] 未找到 .env（也没有模板）：服务可启动，但没有任何厂商密钥。
)

set "PORT=8000"
set "CAND=8000"
if exist providers.yaml for /f "tokens=2 delims=: " %%i in ('findstr /b /c:"port:" providers.yaml 2^>nul') do set "CAND=%%i"
echo %CAND%| findstr /r "^[1-9][0-9]*$" >nul && set "PORT=%CAND%"
set "URL=http://127.0.0.1:%PORT%/"

echo [TTS-Hub] 启动服务：%URL% （就绪后自动打开浏览器；按任意键或关闭窗口停止）
start /b "" ".venv\Scripts\python.exe" -m tts_hub serve

set /a TRIES=0
:waitloop
".venv\Scripts\python.exe" -c "import urllib.request;o=urllib.request.build_opener(urllib.request.ProxyHandler({}));o.open('http://127.0.0.1:%PORT%/api/health',timeout=2)" >nul 2>nul
if not errorlevel 1 goto :opened
set /a TRIES+=1
if %TRIES% GEQ 60 goto :opened
ping -n 2 127.0.0.1 >nul
goto :waitloop

:opened
start "" "%URL%"
echo [TTS-Hub] 已在默认浏览器打开 %URL%
echo [TTS-Hub] 服务运行中：按任意键或直接关闭窗口即可停止。
pause >nul
goto :eof

:fail
echo.
echo [错误] 启动失败，请把上面的输出完整截图或复制后反馈。
pause
exit /b 1
