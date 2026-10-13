@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Preparando entorno Python por primera vez...
  py -3 -m venv .venv
  if errorlevel 1 (echo Instala Python 3.11 o superior. & pause & exit /b 1)
)
echo Instalando/verificando dependencias gratuitas...
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (echo No se pudieron instalar dependencias. Revisa Internet. & pause & exit /b 1)
echo.
echo Gold Engine Fleet abre en http://127.0.0.1:8000
start "" http://127.0.0.1:8000
".venv\Scripts\python.exe" -m uvicorn server:app --host 127.0.0.1 --port 8000
pause
