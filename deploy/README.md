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

## Apache как reverse proxy

Готовый конфиг: [`deploy/apache/kvhomelibrary.conf`](apache/kvhomelibrary.conf).

```bash
sudo a2enmod proxy proxy_http headers ssl remoteip deflate http2

sudo cp deploy/apache/kvhomelibrary.conf \
  /etc/apache2/sites-available/kvhomelibrary.conf

# Замените library.example.com и пути к сертификатам в файле
sudo nano /etc/apache2/sites-available/kvhomelibrary.conf

sudo a2ensite kvhomelibrary
sudo apache2ctl configtest
sudo systemctl reload apache2
```

Приложение должно слушать только loopback:

```bash
sudo KVL_HOST=127.0.0.1 KVL_ADMIN_PASSWORD='надёжный-пароль' \
  bash /tmp/kvhl-install.sh
```

Откройте в firewall только 80/443, порт 8080 наружу не публикуйте.
На RHEL/AlmaLinux при необходимости:

```bash
sudo setsebool -P httpd_can_network_connect 1
```

Сертификат Let's Encrypt:

```bash
sudo apt install certbot python3-certbot-apache
sudo certbot --apache -d library.example.com
```

### Apache на Windows (C:\Apache24)

Готовый конфиг:
[`deploy/apache/kvhomelibrary-windows.conf`](apache/kvhomelibrary-windows.conf).

```powershell
Copy-Item .\deploy\apache\kvhomelibrary-windows.conf `
  C:\Apache24\conf\extra\kvhomelibrary.conf
```

В `C:\Apache24\conf\httpd.conf` добавьте:

```apache
Listen 443
Include conf/extra/kvhomelibrary.conf
```

Если уже включён `conf/extra/httpd-ssl.conf`, закомментируйте его
`Include`, иначе будет второй `Listen 443` и конфликт VirtualHost.

Замените `library.example.com` и пути к сертификатам:

```text
C:\Apache24\conf\ssl\library-fullchain.crt
C:\Apache24\conf\ssl\library.key
```

Проверка и перезапуск:

```powershell
C:\Apache24\bin\httpd.exe -t
Get-Service Apache2.4 | Restart-Service
```

Приложение должно слушать loopback:

```powershell
python -m kvhomelibrary serve `
  --db "C:\KVHomeLibrary\data\library.db" `
  --covers "C:\KVHomeLibrary\data\covers" `
  --library "D:\Books" `
  --host 127.0.0.1 --port 8080 `
  --admin-password "надёжный-пароль"
```

Открывайте в Windows Firewall только 80/443.

Basic Auth для `/admin`:

```powershell
C:\Apache24\bin\htpasswd.exe -c C:\Apache24\conf\kvhomelibrary.htpasswd admin
```

и раскомментируйте блок `<Location "/admin">` в конфиге.

Админка: приложение получает все запросы через Apache с `127.0.0.1`,
поэтому **обязательно** задайте `KVL_ADMIN_PASSWORD` приложения либо
раскомментируйте Basic Auth в конфиге Apache и создайте `htpasswd`.

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
