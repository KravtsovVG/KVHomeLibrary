@echo off
setlocal
cd /d "%~dp0"

if not exist "data" mkdir "data"

set "LIBRARY=%~1"
if "%LIBRARY%"=="" set "LIBRARY=%CD%\books"

python -m kvhomelibrary scan "%LIBRARY%" --db "data\library.db"
if errorlevel 1 pause
