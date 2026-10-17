#requires -Version 5.1
<#
.SYNOPSIS
    Установка/обновление KVHomeLibrary из GitHub на Windows.

.DESCRIPTION
    Клонирует или обновляет проект, создаёт data\ и books\, при необходимости
    сканирует библиотеку и регистрирует автозапуск при входе пользователя
    через Планировщик заданий. Для Linux используйте deploy/linux/install.sh.

.PARAMETER RepositoryUrl
    Git-репозиторий проекта.

.PARAMETER Branch
    Ветка для развёртывания.

.PARAMETER InstallPath
    Куда установить код. По умолчанию %USERPROFILE%\KVHomeLibrary.

.PARAMETER LibraryPath
    Каталог с FB2. По умолчанию <InstallPath>\books.

.PARAMETER BindAddress
    Адрес прослушивания, по умолчанию 0.0.0.0.

.PARAMETER Port
    TCP-порт, по умолчанию 8080.

.PARAMETER SkipScan
    Не сканировать книги при первой установке.

.PARAMETER NoTask
    Не регистрировать задачу автозапуска.

.PARAMETER NoStart
    Не запускать сервер после установки.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\deploy\windows\install.ps1

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\deploy\windows\install.ps1 `
        -LibraryPath D:\Books -Port 8080
#>
[CmdletBinding()]
param(
    [string]$RepositoryUrl = "https://github.com/KravtsovVG/KVHomeLibrary.git",
    [string]$Branch = "main",
    [string]$InstallPath = (Join-Path $env:USERPROFILE "KVHomeLibrary"),
    [string]$LibraryPath = "",
    [string]$BindAddress = "0.0.0.0",
    [int]$Port = 8080,
    [switch]$SkipScan,
    [switch]$NoTask,
    [switch]$NoStart
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Write-Step([string]$Message) {
    Write-Host ""
    Write-Host "==> $Message" -ForegroundColor Cyan
}

function Fail([string]$Message) {
    Write-Host ""
    Write-Host "[x] $Message" -ForegroundColor Red
    exit 1
}

function Invoke-Git {
    param([Parameter(Mandatory = $true)][string[]]$GitArguments)
    & git @GitArguments
    if ($LASTEXITCODE -ne 0) {
        Fail "git $($GitArguments -join ' ') завершился с кодом $LASTEXITCODE"
    }
}

if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    Fail "Git не найден. Установите Git for Windows: https://git-scm.com/download/win"
}

$pythonCommand = Get-Command python -ErrorAction SilentlyContinue
if (-not $pythonCommand) {
    Fail "Python не найден в PATH. Установите Python 3.10+ и включите 'Add python.exe to PATH'."
}
$python = $pythonCommand.Source

& $python -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)"
if ($LASTEXITCODE -ne 0) {
    Fail "Нужен Python 3.10 или новее. Текущий: $(& $python --version 2>&1)"
}

$InstallPath = [System.IO.Path]::GetFullPath($InstallPath)
if ([string]::IsNullOrWhiteSpace($LibraryPath)) {
    $LibraryPath = Join-Path $InstallPath "books"
}
$LibraryPath = [System.IO.Path]::GetFullPath($LibraryPath)

$DataPath = Join-Path $InstallPath "data"
$CoversPath = Join-Path $DataPath "covers"
$DbPath = Join-Path $DataPath "library.db"
$RunnerPath = Join-Path $InstallPath "run-kvhomelibrary.ps1"
$LogPath = Join-Path $DataPath "service.log"
$TaskName = "KVHomeLibrary"
$powershell = (Get-Command powershell.exe -ErrorAction Stop).Source

$existingTask = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existingTask) {
    Write-Step "Останавливаю задачу $TaskName"
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
}

$installParent = Split-Path -Parent $InstallPath
if ($installParent -and -not (Test-Path -LiteralPath $installParent)) {
    New-Item -ItemType Directory -Path $installParent -Force | Out-Null
}

if (Test-Path -LiteralPath (Join-Path $InstallPath ".git")) {
    Write-Step "Обновляю код из $RepositoryUrl ($Branch)"
    Invoke-Git -GitArguments @("-C", $InstallPath, "fetch", "--depth", "1", "origin", $Branch)
    Invoke-Git -GitArguments @("-C", $InstallPath, "reset", "--hard", "origin/$Branch")
} else {
    if ((Test-Path -LiteralPath $InstallPath) -and (Get-ChildItem -LiteralPath $InstallPath -Force | Select-Object -First 1)) {
        Fail "Каталог $InstallPath не пуст и не является git-репозиторием. Укажите другой -InstallPath."
    }
    New-Item -ItemType Directory -Path $InstallPath -Force | Out-Null
    Write-Step "Клонирую $RepositoryUrl ($Branch)"
    Invoke-Git -GitArguments @("clone", "--depth", "1", "--branch", $Branch, $RepositoryUrl, $InstallPath)
}

New-Item -ItemType Directory -Path $DataPath -Force | Out-Null
New-Item -ItemType Directory -Path $CoversPath -Force | Out-Null
New-Item -ItemType Directory -Path $LibraryPath -Force | Out-Null
New-Item -ItemType Directory -Path (Join-Path $LibraryPath "OPDS") -Force | Out-Null

if (-not $SkipScan) {
    $books = Get-ChildItem -LiteralPath $LibraryPath -Recurse -File -Filter *.fb2 -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($books) {
        Write-Step "Первое сканирование библиотеки"
        Push-Location $InstallPath
        try {
            & $python -m kvhomelibrary scan $LibraryPath --db $DbPath --covers $CoversPath
            if ($LASTEXITCODE -ne 0) {
                Fail "Сканирование завершилось с кодом $LASTEXITCODE"
            }
        } finally {
            Pop-Location
        }
    } else {
        Write-Warning "В $LibraryPath нет FB2 — сканирование пропущено."
    }
}

# Отдельный PowerShell-раннер нужен, чтобы stdout/stderr сервера попадали в лог.
$quotedInstall = $InstallPath.Replace("'", "''")
$quotedPython = $python.Replace("'", "''")
$quotedDb = $DbPath.Replace("'", "''")
$quotedCovers = $CoversPath.Replace("'", "''")
$quotedLibrary = $LibraryPath.Replace("'", "''")
$quotedLog = $LogPath.Replace("'", "''")
$runnerLines = @(
    '$ErrorActionPreference = "Stop"',
    "Set-Location -LiteralPath '$quotedInstall'",
    "& '$quotedPython' -m kvhomelibrary serve --db '$quotedDb' --covers '$quotedCovers' --library '$quotedLibrary' --host '$BindAddress' --port $Port *>> '$quotedLog'"
)
Set-Content -LiteralPath $RunnerPath -Value $runnerLines -Encoding UTF8

if (-not $NoTask) {
    Write-Step "Регистрирую автозапуск при входе ($TaskName)"
    $taskArguments = "-NoProfile -ExecutionPolicy Bypass -File `"$RunnerPath`""
    $action = New-ScheduledTaskAction -Execute $powershell -Argument $taskArguments -WorkingDirectory $InstallPath
    $trigger = New-ScheduledTaskTrigger -AtLogOn
    $settings = New-ScheduledTaskSettingsSet -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) `
        -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Seconds 0)
    try {
        Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
            -Settings $settings -Description "KVHomeLibrary — домашняя библиотека FB2 (OPDS)" -Force | Out-Null
    } catch {
        Fail "Не удалось создать задачу автозапуска: $($_.Exception.Message). Запустите PowerShell от администратора или используйте -NoTask."
    }
}

if (-not $NoStart) {
    Write-Step "Запускаю KVHomeLibrary"
    if ($NoTask) {
        Start-Process -FilePath $powershell -ArgumentList "-NoProfile -ExecutionPolicy Bypass -File `"$RunnerPath`"" -WindowStyle Hidden
    } else {
        Start-ScheduledTask -TaskName $TaskName
    }
    Start-Sleep -Seconds 2
}

$ip = $BindAddress
if ($BindAddress -eq "0.0.0.0" -or $BindAddress -eq "::") {
    $ip = (Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
        Where-Object { $_.IPAddress -notlike "127.*" -and $_.IPAddress -notlike "169.254.*" } |
        Select-Object -First 1 -ExpandProperty IPAddress)
    if (-not $ip) { $ip = "127.0.0.1" }
}

Write-Host ""
Write-Host "Готово. KVHomeLibrary установлен." -ForegroundColor Green
Write-Host "  Код:       $InstallPath"
Write-Host "  База:      $DataPath"
Write-Host "  Книги:     $LibraryPath"
Write-Host "  Лог:       $LogPath"
Write-Host "  Веб:       http://${ip}:$Port/"
Write-Host "  OPDS:      http://${ip}:$Port/opds"
if ($NoTask) {
    Write-Host "  Автозапуск: не настроен (-NoTask)"
} else {
    Write-Host "  Автозапуск: задача Планировщика $TaskName (при входе пользователя)"
    Write-Host "  Остановить: Stop-ScheduledTask -TaskName $TaskName"
}
Write-Host "  Обновление: powershell -ExecutionPolicy Bypass -File `"$InstallPath\deploy\windows\install.ps1`""
