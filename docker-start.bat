@echo off
chcp 65001 >nul
cd /d "%~dp0"
rem Сборка (если код изменился) и запуск контейнера в фоне. Docker Desktop должен быть запущен.
if not exist ".env" (
  echo Нет файла .env. Скопируйте .env.example в .env и впишите адреса камер.
  pause & exit /b 1
)
if not exist "data" mkdir data
docker compose up -d --build
if errorlevel 1 (
  echo.
  echo Не удалось запустить. Проверьте, что Docker Desktop запущен.
  pause & exit /b 1
)
echo.
echo Готово. Сайт: http://localhost:8000
echo Логи: docker-logs.bat   Остановка: docker-stop.bat
pause
