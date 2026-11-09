@echo off
chcp 65001 >nul
rem Удаление службы. Запускать от имени администратора. База и фото остаются в папке data.
net session >nul 2>&1
if errorlevel 1 (
  echo Нужны права администратора.
  pause & exit /b 1
)
set "NSSM=nssm"
where nssm >nul 2>&1 || (if exist "%~dp0nssm.exe" set "NSSM=%~dp0nssm.exe")
"%NSSM%" stop Shlagbaum
"%NSSM%" remove Shlagbaum confirm
netsh advfirewall firewall delete rule name="Shlagbaum 8000" >nul 2>&1
echo Служба удалена.
pause
