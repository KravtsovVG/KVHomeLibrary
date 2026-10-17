@echo off
setlocal
cd /d "%~dp0"

net session >nul 2>&1
if errorlevel 1 (
    echo.
    echo   Требуются права администратора.
    echo   Щелкните правой кнопкой по файлу allow-firewall.bat
    echo   и выберите "Запуск от имени администратора".
    echo.
    pause
    exit /b 1
)

netsh advfirewall firewall delete rule name="KVHomeLibrary OPDS 8080" >nul 2>&1
netsh advfirewall firewall add rule name="KVHomeLibrary OPDS 8080" dir=in action=allow protocol=TCP localport=8080 profile=private,domain
if errorlevel 1 (
    echo.
    echo   Не удалось добавить правило. Проверьте, что сеть Windows
    echo   имеет профиль "Частная" ^(Private^), и повторите запуск.
    echo.
    pause
    exit /b 1
)

echo.
echo   Готово: входящие подключения на TCP-порт 8080 разрешены
echo   для частных и доменных сетей.
echo.
pause
