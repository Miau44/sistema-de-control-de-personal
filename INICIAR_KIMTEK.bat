@echo off
cd /d "%~dp0"
set "ASISTENCIA_DB=%~dp0instance\kimtek.sqlite3"
set "AUTO_CLOSE=0"
if not exist "%ASISTENCIA_DB%" (
  echo Primero prepara la base siguiendo PREPARACION_ENTORNO_LOCAL_KIMTEK.md
  pause
  exit /b 1
)
"%~dp0.venv\Scripts\python.exe" -m uvicorn app:app --host 127.0.0.1 --port 8000
