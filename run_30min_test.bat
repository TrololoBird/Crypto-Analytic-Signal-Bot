@echo off
chcp 65001 >nul
setlocal

set RUNTIME_SECONDS=1800
if not "%~1"=="" set RUNTIME_SECONDS=%~1

echo ========================================
echo  BOT LIVE SMOKE TEST
echo ========================================
echo Runtime seconds: %RUNTIME_SECONDS%
echo Telegram sends: disabled, fake broadcaster
echo.

python scripts\live_smoke_bot.py --runtime-seconds %RUNTIME_SECONDS% --shutdown-timeout-seconds 90 --force-exit-on-close-timeout
exit /b %ERRORLEVEL%
