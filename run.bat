@echo off
rem Corre el clipper 24/7: si se cae, lo relanza. Para salir: Ctrl+C y responder S.
cd /d "%~dp0"
if not exist logs mkdir logs
set "PY=.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"
set PYTHONUTF8=1

:loop
"%PY%" main.py
set "CODE=%errorlevel%"
rem 0 = salida con Ctrl+C, 2 = error de configuracion, 3 = ya hay otra instancia: no relanzar
if "%CODE%"=="0" goto fin
if "%CODE%"=="2" goto fin
if "%CODE%"=="3" goto fin
echo [%date% %time%] main.py salio con codigo %CODE%, relanzo en 15 s >> logs\run_bat.log
timeout /t 15 /nobreak >nul
goto loop

:fin
echo [%date% %time%] main.py salio con codigo %CODE%, no se relanza (ver logs\clipper.log) >> logs\run_bat.log
exit /b %CODE%
