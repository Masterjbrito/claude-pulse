@echo off
REM ============================================
REM  Claude Pulse - Windows installer
REM  Installs deps + autostart with Windows
REM ============================================
echo.
echo  Claude Pulse - instalacao / install
echo.

where python >nul 2>nul
if errorlevel 1 (
  echo [ERRO] Python nao encontrado. Instala em https://python.org e tenta de novo.
  echo [ERROR] Python not found. Install from https://python.org and retry.
  pause & exit /b 1
)

echo A instalar dependencias / installing dependencies...
python -m pip install --quiet -r "%~dp0requirements.txt"

REM registar arranque automatico: um vbs no Startup que aponta para o launcher portatil
set "SCRIPT_DIR=%~dp0"
set "VBS=%SCRIPT_DIR%ClaudePulse.vbs"
set "STARTUP_VBS=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\ClaudePulse.vbs"
> "%STARTUP_VBS%" echo Set sh = CreateObject("WScript.Shell")
>> "%STARTUP_VBS%" echo sh.Run "wscript ""%VBS%""", 0, False

echo.
echo  Instalado! Arranca automaticamente com o Windows.
echo  Installed! Starts automatically with Windows.
echo  A iniciar agora / starting now...
start "" wscript "%VBS%"
timeout /t 3 >nul
