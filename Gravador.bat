@echo off
REM Abre o gravador mostrando o console (util para ver erros).
cd /d "%~dp0"
python "%~dp0app.py"
if errorlevel 1 pause
