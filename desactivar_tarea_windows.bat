@echo off
REM Desactiva la tarea de Windows "Previsor precios OMIE" (el previsor ya se ejecuta en GitHub).
REM Para volver a activarla: schtasks /Change /TN "Previsor precios OMIE" /ENABLE
schtasks /Change /TN "Previsor precios OMIE" /DISABLE
pause
