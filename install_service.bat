@echo off
chcp 65001 >nul
rem Установка системы учёта проездов как службы Windows (автозапуск и перезапуск при сбое).
rem Запускать ПРАВОЙ КНОПКОЙ -> "Запуск от имени администратора".
cd /d "%~dp0"

net session >nul 2>&1
if errorlevel 1 (
  echo Нужны права администратора: правой кнопкой по файлу -^> "Запуск от имени администратора".
  pause & exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  echo Устанавливаю зависимости...
  py -3.11 -m venv .venv || (echo Не найден Python 3.11 & pause & exit /b 1)
  .venv\Scripts\python.exe -m pip install -r requirements.txt || (pause & exit /b 1)
)

where nssm >nul 2>&1
if errorlevel 1 (
  echo Устанавливаю NSSM через winget...
  winget install -e --id NSSM.NSSM --accept-source-agreements --accept-package-agreements
  for /f "delims=" %%i in ('where /r "%LOCALAPPDATA%\Microsoft\WinGet\Packages" nssm.exe 2^>nul ^| findstr /i win64') do set "NSSM=%%i"
) else (
  for /f "delims=" %%i in ('where nssm') do set "NSSM=%%i"
)
if not defined NSSM (
  echo NSSM не найден. Скачайте с https://nssm.cc/download и положите nssm.exe рядом с этим файлом.
  if exist "%~dp0nssm.exe" (set "NSSM=%~dp0nssm.exe") else (pause & exit /b 1)
)

set SVC=Shlagbaum
"%NSSM%" stop %SVC% >nul 2>&1
"%NSSM%" remove %SVC% confirm >nul 2>&1
"%NSSM%" install %SVC% "%~dp0.venv\Scripts\python.exe" run.py
"%NSSM%" set %SVC% AppDirectory "%~dp0."
"%NSSM%" set %SVC% DisplayName "Шлагбаум: учёт проездов"
"%NSSM%" set %SVC% Description "Распознавание номеров, белый список, журнал въезда/выезда"
"%NSSM%" set %SVC% Start SERVICE_AUTO_START
"%NSSM%" set %SVC% AppExit Default Restart
"%NSSM%" set %SVC% AppRestartDelay 5000
"%NSSM%" set %SVC% AppStdout "%~dp0data\service.log"
"%NSSM%" set %SVC% AppStderr "%~dp0data\service.log"
"%NSSM%" set %SVC% AppRotateFiles 1
"%NSSM%" set %SVC% AppRotateBytes 10485760
rem модели распознавания берутся из кэша текущего пользователя (не скачиваются заново)
"%NSSM%" set %SVC% AppEnvironmentExtra "USERPROFILE=%USERPROFILE%" "PYTHONIOENCODING=utf-8"
"%NSSM%" start %SVC%

echo.
set /p FW="Открыть порт 8000 в брандмауэре, чтобы интерфейс открывался с других компьютеров? (y/n): "
if /i "%FW%"=="y" netsh advfirewall firewall add rule name="Shlagbaum 8000" dir=in action=allow protocol=TCP localport=8000

echo.
echo Готово. Служба "%SVC%" запущена и будет стартовать вместе с Windows.
echo Интерфейс: http://localhost:8000
pause
