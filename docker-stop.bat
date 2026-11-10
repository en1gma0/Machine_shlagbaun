@echo off
chcp 65001 >nul
cd /d "%~dp0"
rem Остановка контейнера. База, фото и настройки остаются в папке data и файле .env.
docker compose down
pause
