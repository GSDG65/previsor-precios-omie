@echo off
REM Evalua que modelo meteorologico acierta mejor el viento y el sol a 1-5 dias (Open-Meteo, Previous Runs API)
REM No modifica el previsor: solo crea datos\meteo_modelos\, datos\meteo_modelos_indices.csv y datos\evaluacion_meteo_modelos.csv
cd /d "%~dp0"
set "PY=%LOCALAPPDATA%\Microsoft\WindowsApps\python.exe"
if not exist "%PY%" set "PY=%LOCALAPPDATA%\Microsoft\WindowsApps\py.exe"
"%PY%" evaluar_meteo.py %*
pause
