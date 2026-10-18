# Развёртывание KVHomeLibrary

Скрипты клонируют проект из GitHub, создают каталоги базы и книг и запускают сервер.

- **Linux:** systemd-демон, автозапуск при загрузке.
- **Windows:** Планировщик заданий, автозапуск при входе пользователя.

## Linux (Debian/Ubuntu)

Требования: Debian/Ubuntu с systemd, root/sudo, доступ в интернет.

```bash
curl -fsSL https://raw.githubusercontent.com/KravtsovVG/KVHomeLibrary/main/deploy/linux/install.sh \
  -o /tmp/kvhl-install.sh

sudo bash /tmp/kvhl-install.sh
```

Скрипт установит `git`/Python при необходимости, создаст пользователя `kvhome`,
каталоги, systemd-юнит, выполнит первое сканирование и запустит демон.

Для импорта 7Z/RAR установите внешний распаковщик:

```bash
sudo apt install p7zip-full unrar
```

### Пути по умолчанию

| Что | Где |
| --- | --- |
| Код | `/opt/kvhomelibrary` |
| База и обложки | `/var/lib/kvhomelibrary` |
| Книги FB2 | `/srv/books` |
| Скачанные OPDS-книги | `/srv/books/OPDS` |
| Сервис | `kvhomelibrary.service` |
| Порт | `8080` |

### Свой каталог книг

```bash
sudo KVL_LIBRARY_DIR=/mnt/books bash /tmp/kvhl-install.sh
```

Не размещайте книги в `/home`: systemd-юнит использует `ProtectHome=true`.

### Доступ к администрированию по сети

По умолчанию `/admin` доступен только с localhost. Чтобы открыть его по сети:

```bash
sudo KVL_ADMIN_PASSWORD='надёжный-пароль' bash /tmp/kvhl-install.sh
```

Пароль сохраняется в `/etc/kvhomelibrary.env` с правами `0600` и подхватывается
systemd. После изменения файла:

```bash
sudo systemctl restart kvhomelibrary
```

### Управление

```bash
sudo systemctl status kvhomelibrary
sudo systemctl restart kvhomelibrary
sudo journalctl -u kvhomelibrary -f
```

### Обновление из GitHub

Повторно запустите установщик:

```bash
sudo bash /opt/kvhomelibrary/deploy/linux/install.sh
```

Скрипт останавливает сервис, делает `git fetch`/`reset --hard origin/main`,
переустанавливает юнит, запускает демон и проверяет `/healthz`. База, обложки и
книги не удаляются.

### Параметры Linux-скрипта

| Переменная | По умолчанию |
| --- | --- |
| `KVL_REPO_URL` | `https://github.com/KravtsovVG/KVHomeLibrary.git` |
| `KVL_BRANCH` | `main` |
| `KVL_INSTALL_DIR` | `/opt/kvhomelibrary` |
| `KVL_DATA_DIR` | `/var/lib/kvhomelibrary` |
| `KVL_LIBRARY_DIR` | `/srv/books` |
| `KVL_SERVICE_USER` | `kvhome` |
| `KVL_PYTHON` | `python3` |
| `KVL_HOST` | `0.0.0.0` |
| `KVL_PORT` | `8080` |
| `KVL_ADMIN_PASSWORD` | не задан |
| `KVL_SKIP_SCAN` | `0` |

## Windows

Требования: Windows 10/11, Git for Windows, Python 3.10+ в `PATH`.
Для импорта 7Z/RAR установите [7-Zip](https://www.7-zip.org/).

Откройте PowerShell:

```powershell
git clone https://github.com/KravtsovVG/KVHomeLibrary.git "$env:USERPROFILE\KVHomeLibrary"
Set-Location "$env:USERPROFILE\KVHomeLibrary"

powershell -ExecutionPolicy Bypass -File .\deploy\windows\install.ps1 `
  -LibraryPath "D:\Books"
```

Если Git ещё не установлен, скачайте проект ZIP-архивом с GitHub, распакуйте и
запустите `install.ps1` из распакованного каталога — но для обновлений нужен Git.

### Что делает скрипт

- клонирует или обновляет код;
- создаёт `data\`, `data\covers`, `books\`;
- при необходимости сканирует библиотеку;
- создаёт `run-kvhomelibrary.ps1` с логом `data\service.log`;
- регистрирует задачу Планировщика `KVHomeLibrary` с запуском при входе;
- запускает сервер.

### Пути по умолчанию

| Что | Где |
| --- | --- |
| Код | `%USERPROFILE%\KVHomeLibrary` |
| База | `%USERPROFILE%\KVHomeLibrary\data\library.db` |
| Книги | `%USERPROFILE%\KVHomeLibrary\books` |
| Лог | `%USERPROFILE%\KVHomeLibrary\data\service.log` |
| Порт | `8080` |

### Управление задачей

```powershell
Get-ScheduledTask -TaskName KVHomeLibrary
Stop-ScheduledTask -TaskName KVHomeLibrary
Start-ScheduledTask -TaskName KVHomeLibrary
```

### Обновление

```powershell
powershell -ExecutionPolicy Bypass -File "$env:USERPROFILE\KVHomeLibrary\deploy\windows\install.ps1"
```

Скрипт останавливает задачу, обновляет код и запускает её снова. База и книги
сохраняются.

### Параметры Windows-скрипта

| Параметр | По умолчанию |
| --- | --- |
| `-RepositoryUrl` | `https://github.com/KravtsovVG/KVHomeLibrary.git` |
| `-Branch` | `main` |
| `-InstallPath` | `%USERPROFILE%\KVHomeLibrary` |
| `-LibraryPath` | `<InstallPath>\books` |
| `-BindAddress` | `0.0.0.0` |
| `-Port` | `8080` |
| `-SkipScan` | сканировать при первом запуске |
| `-NoTask` | не создавать задачу автозапуска |
| `-NoStart` | не запускать сервер |

Пример без автозапуска:

```powershell
powershell -ExecutionPolicy Bypass -File .\deploy\windows\install.ps1 `
  -LibraryPath "D:\Books" -NoTask
```

## Проверка после установки

- Веб-интерфейс: `http://<IP>:8080/`
- OPDS: `http://<IP>:8080/opds`
- Здоровье сервиса: `http://<IP>:8080/healthz`

## Удаление

Linux:

```bash
sudo systemctl disable --now kvhomelibrary
sudo rm /etc/systemd/system/kvhomelibrary.service
sudo systemctl daemon-reload
```

Windows:

```powershell
Stop-ScheduledTask -TaskName KVHomeLibrary
Unregister-ScheduledTask -TaskName KVHomeLibrary -Confirm:$false
```

Каталоги `data`, `books` и `/var/lib/kvhomelibrary` удаляются вручную, если они
больше не нужны.
