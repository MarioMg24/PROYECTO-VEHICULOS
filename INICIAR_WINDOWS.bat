@echo off
setlocal EnableExtensions DisableDelayedExpansion
cd /d "%~dp0"
if errorlevel 1 exit /b 1
set "FLEET_PYTHON=%~dp0.venv-sqlite-fixed\Scripts\python.exe"
set "PYTHONPATH="
set "PYTHONDONTWRITEBYTECODE=1"
set "GEFLEET_ALLOW_CREATE_DB="

if not exist "%FLEET_PYTHON%" (
  echo ERROR: No existe .venv-sqlite-fixed\Scripts\python.exe.
  echo Prepara el entorno corregido siguiendo ACTUALIZACION_SQLITE_2026-10-13.md.
  goto :startup_failed
)

echo Verificando el entorno corregido...
"%FLEET_PYTHON%" -B -c "import sys,sqlite3; print('Python:',sys.version.split()[0]); print('SQLite:',sqlite3.sqlite_version); sys.exit(0 if sys.version_info[:3] == (3,15,0) and sqlite3.sqlite_version == '3.53.4' else 1)"
if errorlevel 1 (
  echo ERROR: Se requiere el entorno probado con Python 3.15.0 y SQLite 3.53.4.
  goto :startup_failed
)

"%FLEET_PYTHON%" -B -c "import fastapi,uvicorn"
if errorlevel 1 (
  echo ERROR: FastAPI o Uvicorn no estan disponibles en el entorno corregido.
  echo Revisa las instrucciones de instalacion; el arranque no instala dependencias.
  goto :startup_failed
)

if /i "%~1"=="--check" (
  echo Entorno verificado. No se inicio Uvicorn ni se abrio una base de datos.
  exit /b 0
)

"%FLEET_PYTHON%" -B -c "import socket; sock=socket.socket(); sock.bind(('127.0.0.1',8000)); sock.close()"
if errorlevel 1 (
  echo ERROR: No se puede utilizar 127.0.0.1:8000. Comprueba si ya hay un servidor activo.
  goto :startup_failed
)

echo.
echo Gold Engine Fleet: abre http://127.0.0.1:8000 cuando Uvicorn indique que esta listo.
"%FLEET_PYTHON%" -B -m uvicorn server:app --host 127.0.0.1 --port 8000
set "FLEET_EXIT_CODE=%ERRORLEVEL%"
if not "%FLEET_EXIT_CODE%"=="0" echo ERROR: El servidor termino con codigo %FLEET_EXIT_CODE%.
pause
exit /b %FLEET_EXIT_CODE%

:startup_failed
if /i not "%~1"=="--check" pause
exit /b 1
