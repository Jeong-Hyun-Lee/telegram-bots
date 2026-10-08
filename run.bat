@echo off
chcp 65001 > nul
set PYTHONUTF8=1
cd /d "%~dp0"
if "%~1"=="" (
    echo 사용법: run.bat ^<봇이름^>   예: run.bat nara
    echo 봇 목록:
    dir /b bots
    pause
    exit /b 1
)
python bot.py %1
pause
