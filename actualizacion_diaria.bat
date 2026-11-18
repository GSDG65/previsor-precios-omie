@echo off
REM Actualizacion diaria del previsor de precios OMIE (la lanza el Programador de tareas)
REM Python se llama por su ruta exacta para no recorrer otras carpetas ni discos del sistema.
cd /d "%~dp0"
set "PY=%LOCALAPPDATA%\Microsoft\WindowsApps\python.exe"
if not exist "%PY%" set "PY=%LOCALAPPDATA%\Microsoft\WindowsApps\py.exe"
"%PY%" actualizacion_diaria.py %*
