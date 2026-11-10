@echo off
chcp 65001 >nul
cd /d "%~dp0"
rem Последние строки лога и дальше в реальном времени. Выход: Ctrl+C
docker compose logs -f --tail 100
