@echo off
setlocal
cd /d "%~dp0"

if not exist "data" mkdir "data"
if not exist "books" mkdir "books"

set "LIBRARY=%~1"
if "%LIBRARY%"=="" set "LIBRARY=%CD%\books"

python -m kvhomelibrary serve --db "data\library.db" --library "%LIBRARY%" --scan --host 0.0.0.0
if errorlevel 1 pause
