@echo off
chcp 65001 >nul
echo.
echo ========================================
echo   艾薇AI助理 LINE 主管回報機器人
echo ========================================
echo.

cd /d "%~dp0"

REM 讀取 .env 環境變數
for /f "tokens=1,2 delims==" %%a in (.env) do (
    if not "%%a"=="" if not "%%b"=="" set %%a=%%b
)

REM 啟動 Flask 在背景
echo [1/2] 啟動 Flask 伺服器 (port 5000)...
start /B python bot.py > bot.log 2>&1
timeout /t 2 /nobreak >nul

REM 啟動 ngrok
echo [2/2] 啟動 ngrok 取得公開網址...
echo.
echo ⚠️  請複製下方 ngrok 網址並加上 /webhook
echo     填入 LINE Developers Console 的 Webhook URL
echo.
ngrok http 5000
