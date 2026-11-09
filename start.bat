@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist .venv (
  echo Первый запуск: устанавливаю зависимости...
  py -3.11 -m venv .venv
  .venv\Scripts\python.exe -m pip install -r requirements.txt
)
echo Откройте http://localhost:8000
.venv\Scripts\python.exe run.py
pause
