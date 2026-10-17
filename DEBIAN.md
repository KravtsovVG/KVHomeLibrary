# Запуск KVHomeLibrary на Debian

Инструкция проверена для Debian 12 и 13. Проекту нужен только Python 3.10+
и стандартная библиотека — `pip`, виртуальное окружение и сторонние пакеты
Python не требуются.

Ниже используется такая схема:

| Что | Где |
| --- | --- |
| Код проекта | `/opt/kvhomelibrary` |
| База и обложки | `/var/lib/kvhomelibrary` |
| Книги FB2 | `/srv/books` |
| Пользователь сервиса | `kvhome` |
| Порт | `8080` |

Если у вас другие пути — замените их в командах и в systemd-юните.

## 1. Подготовка системы

```bash
sudo apt update
sudo apt install -y python3 rsync curl
python3 --version
```

Версия должна быть **3.10 или новее**. В Debian 12 это Python 3.11,
в Debian 13 — 3.13.

## 2. Размещение проекта

Скопируйте папку `KVHomeLibrary` на Debian, например в `/tmp`, и перенесите
код в `/opt`:

```bash
sudo install -d -m 755 /opt/kvhomelibrary
sudo rsync -a --exclude '.git' --exclude 'data' --exclude 'covers' \
    /tmp/KVHomeLibrary/ /opt/kvhomelibrary/
```

Если проект уже лежит на Debian в другом месте, просто скопируйте его
содержимое в `/opt/kvhomelibrary`.

Код должен принадлежать `root`, а читать его будет сервисный пользователь:

```bash
sudo chown -R root:root /opt/kvhomelibrary
sudo find /opt/kvhomelibrary -type d -exec chmod 755 {} +
sudo find /opt/kvhomelibrary -type f -exec chmod 644 {} +
```

Проверка:

```bash
cd /opt/kvhomelibrary
python3 -m kvhomelibrary --version
```

Должно вывести `kvhomelibrary 0.6.9` (или более новую версию).

## 3. Пользователь и каталоги

Создайте системного пользователя без возможности входа:

```bash
sudo adduser --system --group --home /var/lib/kvhomelibrary \
    --shell /usr/sbin/nologin kvhome
```

Если пользователь уже существует, команда сообщит об этом — это нормально.

Создайте каталоги для базы, обложек и книг:

```bash
sudo install -d -o kvhome -g kvhome -m 750 /var/lib/kvhomelibrary
sudo install -d -o kvhome -g kvhome -m 750 /var/lib/kvhomelibrary/covers
sudo install -d -o root   -g kvhome -m 750 /srv/books
sudo install -d -o kvhome -g kvhome -m 750 /srv/books/OPDS
```

Каталог `/srv/books/OPDS` нужен для книг, скачанных из внешних OPDS-источников:
сервис должен иметь право писать в него.

Положите книги в `/srv/books` (можно в подпапки). Пользователь `kvhome`
должен иметь право читать файлы:

```bash
sudo -u kvhome test -r /srv/books && echo "чтение /srv/books разрешено"
```

Если книги принадлежат другому пользователю и закрыты от чтения, дайте
доступ группе `kvhome` через ACL:

```bash
sudo apt install -y acl
sudo setfacl -R -m g:kvhome:rX /srv/books
sudo setfacl -R -d -m g:kvhome:rX /srv/books
```

## 4. Первое сканирование

Запускайте команды из каталога проекта:

```bash
cd /opt/kvhomelibrary

sudo -u kvhome env PYTHONDONTWRITEBYTECODE=1 \
    python3 -m kvhomelibrary scan /srv/books \
    --db /var/lib/kvhomelibrary/library.db \
    --covers /var/lib/kvhomelibrary/covers
```

Проверьте результат:

```bash
sudo -u kvhome env PYTHONDONTWRITEBYTECODE=1 \
    python3 -m kvhomelibrary stats \
    --db /var/lib/kvhomelibrary/library.db
```

Должны появиться количество книг, авторов, категорий и серий. Если есть
ошибки разбора, команда `stats` покажет первые проблемные файлы.

## 5. Проверка сервера вручную

Запустите сервер в переднем плане:

```bash
cd /opt/kvhomelibrary

sudo -u kvhome env PYTHONDONTWRITEBYTECODE=1 \
    python3 -m kvhomelibrary serve \
    --db /var/lib/kvhomelibrary/library.db \
    --covers /var/lib/kvhomelibrary/covers \
    --library /srv/books \
    --host 0.0.0.0 --port 8080
```

В другом терминале:

```bash
curl -I http://127.0.0.1:8080/opds
hostname -I
```

`curl` должен вернуть `200 OK` и тип
`application/atom+xml;profile=opds-catalog;kind=navigation`.
`hostname -I` покажет IP компьютера в локальной сети.

Остановите сервер через `Ctrl+C`. Для постоянной работы настройте systemd.

## 6. systemd-сервис

Создайте файл `/etc/systemd/system/kvhomelibrary.service`:

```bash
sudo nano /etc/systemd/system/kvhomelibrary.service
```

Содержимое:

```ini
[Unit]
Description=KVHomeLibrary — домашняя библиотека FB2 (OPDS)
Documentation=file:/opt/kvhomelibrary/README.md
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=kvhome
Group=kvhome
WorkingDirectory=/opt/kvhomelibrary
Environment=PYTHONDONTWRITEBYTECODE=1
Environment=PYTHONUNBUFFERED=1
EnvironmentFile=-/etc/kvhomelibrary.env
ExecStart=/usr/bin/python3 -m kvhomelibrary serve --db /var/lib/kvhomelibrary/library.db --covers /var/lib/kvhomelibrary/covers --library /srv/books --host 0.0.0.0 --port 8080
Restart=on-failure
RestartSec=5
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/var/lib/kvhomelibrary /srv/books/OPDS
UMask=0027

[Install]
WantedBy=multi-user.target
```

Если книги лежат на отдельном разделе или сетевой шаре, раскомментируйте
и укажите путь:

```ini
RequiresMountsFor=/srv/books
```

и при необходимости замените `/srv/books/OPDS` на свой путь для скачанных книг.

Включите и запустите:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now kvhomelibrary
sudo systemctl status --no-pager kvhomelibrary
```

Логи:

```bash
sudo journalctl -u kvhomelibrary -f
```

После изменения книг в `/srv/books` выполните сканирование вручную
(команда из раздела 4) или настройте cron из раздела 9. Сервис при
запуске не сканирует библиотеку, чтобы не задерживать старт.

### 6.1. Веб-каталог и администрирование

После запуска откройте:

```text
http://<IP-сервера>:8080/
```

Разделы:

| Адрес | Назначение |
| --- | --- |
| `/` | главная страница с поиском |
| `/ui/books` | книги, поиск и фильтры |
| `/ui/authors` | авторы |
| `/ui/genres` | категории |
| `/ui/series` | серии |
| `/admin` | администрирование |

По умолчанию `/admin` доступен только с самого сервера, то есть при
открытии `http://127.0.0.1:8080/admin`. Чтобы управлять каталогом с другого
компьютера или телефона, задайте пароль.

Создайте файл с паролем (доступен только root):

```bash
sudo install -m 600 /dev/null /etc/kvhomelibrary.env
echo 'KVL_ADMIN_PASSWORD=смените-этот-пароль' | sudo tee /etc/kvhomelibrary.env
sudo systemctl restart kvhomelibrary
```

Теперь при открытии `/admin` браузер запросит логин и пароль. Логин может
быть любым, проверяется только пароль.

В администрировании можно редактировать метаданные книги, удалять записи
из каталога, запускать сканирование и перестраивать поисковый индекс.
Удаление не удаляет FB2-файл с диска. Ручные правки сохраняются в SQLite;
неизменённые файлы при сканировании пропускаются, но изменённый FB2-файл
заменит ручные правки своими метаданными.

Basic Auth передаёт пароль по HTTP без шифрования. Используйте его только
в доверенной локальной сети; для доступа через интернет поставьте сервер
за HTTPS-прокси (nginx, Caddy) или используйте SSH-туннель.

### 6.2. Внешние источники OPDS

В `/admin/sources` можно добавить чужие OPDS-каталоги, просматривать их
и скачивать FB2 в свою библиотеку. Источники хранятся в SQLite в таблице
`opds_sources`.

Для скачивания сервисный пользователь `kvhome` должен иметь право писать
в `/srv/books/OPDS`:

```bash
sudo install -d -o kvhome -g kvhome -m 750 /srv/books/OPDS
sudo systemctl restart kvhomelibrary
```

В systemd-юните это каталог уже включён в `ReadWritePaths`. Скачанные
книги автоматически разбираются и добавляются в каталог; полное
сканирование для этого не требуется.

Поддерживаются только FB2 (`application/fb2+xml` и `application/fb2+zip`).
Логин и пароль источника хранятся в локальной базе в открытом виде —
используйте отдельный пароль для OPDS, а не основной пароль аккаунта.
Управление источниками доступно только после входа в `/admin`.

## 7. Брандмауэр

Debian часто не включает ufw по умолчанию. Если он используется, откройте
порт 8080 только для своей локальной подсети. Сначала узнайте подсеть:

```bash
ip -4 addr show
```

Например, если адрес компьютера `192.168.55.101/24`, подсеть —
`192.168.55.0/24`:

```bash
sudo apt install -y ufw
sudo ufw allow from 192.168.55.0/24 to any port 8080 proto tcp
sudo ufw enable
sudo ufw status numbered
```

Не открывайте порт 8080 в интернет: у сервера нет аутентификации.

Если используется `nftables` без ufw, добавьте аналогичное правило для
TCP-порта 8080, разрешив только локальную подсеть.

## 8. Доступ с телефона или другого компьютера

1. Убедитесь, что сервис запущен:
   ```bash
   sudo systemctl status --no-pager kvhomelibrary
   ```
2. Узнайте IP:
   ```bash
   hostname -I
   ```
3. На компьютере проверьте:
   ```bash
   curl -I http://127.0.0.1:8080/opds
   curl -I http://192.168.55.101:8080/opds
   ```
   Второй адрес замените на свой.
4. В OPDS-читалке укажите:
   ```text
   http://192.168.55.101:8080/opds
   ```

Корень `http://192.168.55.101:8080/` тоже работает: браузеру отдаётся
HTML-страница, OPDS-клиенту — XML-каталог.

Телефон и компьютер должны быть в одной сети. Гостевая Wi-Fi-сеть и
изоляция клиентов на роутере могут блокировать доступ даже при открытом
порту.

## 9. Автоматическое сканирование по расписанию

Добавьте задачу в cron пользователя `kvhome`:

```bash
sudo crontab -u kvhome -e
```

Строка — сканирование каждый день в 04:00:

```cron
0 4 * * * cd /opt/kvhomelibrary && PYTHONDONTWRITEBYTECODE=1 /usr/bin/python3 -m kvhomelibrary scan /srv/books --db /var/lib/kvhomelibrary/library.db --covers /var/lib/kvhomelibrary/covers --prune >> /var/lib/kvhomelibrary/scan.log 2>&1
```

`--prune` удаляет из базы записи о файлах, которых больше нет. Если это
не нужно, уберите флаг. Не используйте `--prune`, если сетевая шара может
быть не смонтирована в момент сканирования: иначе все книги могут пропасть
из каталога.

Проверить лог:

```bash
sudo tail -f /var/lib/kvhomelibrary/scan.log
```

## 10. Обновление проекта

```bash
sudo systemctl stop kvhomelibrary
sudo rsync -a --delete --exclude 'data' --exclude 'covers' \
    /tmp/KVHomeLibrary/ /opt/kvhomelibrary/
sudo chown -R root:root /opt/kvhomelibrary
sudo systemctl start kvhomelibrary
sudo systemctl status --no-pager kvhomelibrary
```

База и обложки находятся в `/var/lib/kvhomelibrary` и при обновлении кода
не удаляются. Если меняется схема БД, перед обновлением сделайте резервную
копию:

```bash
sudo systemctl stop kvhomelibrary
sudo tar czf /var/backups/kvhomelibrary-$(date +%F).tar.gz \
    /var/lib/kvhomelibrary
sudo systemctl start kvhomelibrary
```

## 11. Необязательно: книги на SMB-шаре

Если библиотека лежит на NAS с SMB/CIFS:

```bash
sudo apt install -y cifs-utils
sudo install -d -o root -g kvhome -m 750 /srv/books
```

Создайте файл с учётными данными:

```bash
sudo nano /etc/cifs-books.cred
```

```ini
username=bookuser
password=секрет
```

```bash
sudo chmod 600 /etc/cifs-books.cred
```

Строка для `/etc/fstab`:

```fstab
//192.168.55.10/Books /srv/books cifs credentials=/etc/cifs-books.cred,uid=kvhome,gid=kvhome,ro,iocharset=utf8,nofail 0 0
```

Замените адрес NAS и имя ресурса. После этого:

```bash
sudo systemctl daemon-reload
sudo mount -a
sudo systemctl restart kvhomelibrary
```

Если нужны загрузки из внешних OPDS, шара должна быть смонтирована
с правом записи (`rw`), иначе сервис не сможет сохранить книгу
в `/srv/books/OPDS`.

## 12. Если что-то не работает

### `ModuleNotFoundError: No module named 'kvhomelibrary'`

Запускайте команды из каталога проекта:

```bash
cd /opt/kvhomelibrary
python3 -m kvhomelibrary --version
```

В systemd для этого указан `WorkingDirectory=/opt/kvhomelibrary`.

### `PermissionError` при записи `library.db` или обложек

```bash
sudo chown -R kvhome:kvhome /var/lib/kvhomelibrary
sudo systemctl restart kvhomelibrary
```

### `Address already in use`

Порт 8080 занят другим процессом:

```bash
sudo ss -ltnp 'sport = :8080'
```

Остановите старый экземпляр или запустите сервис на другом порту
(`--port 8081`) и не забудьте поменять порт в ссылках и брандмауэре.

### Сервис не запускается

```bash
sudo systemctl status --no-pager kvhomelibrary
sudo journalctl -u kvhomelibrary -n 100 --no-pager
```

Частые причины: неверный путь к базе, нет прав на `/srv/books`,
занят порт, книги на не смонтированном разделе.

### С телефона `connection timed out`, а с компьютера всё работает

Проверьте:

```bash
hostname -I
sudo ufw status
sudo ss -ltnp 'sport = :8080'
```

Сервер должен слушать `0.0.0.0:8080`, а не `127.0.0.1:8080`.
Убедитесь, что телефон в той же подсети и что Wi-Fi не гостевой.

### Неверный IP

`hostname -I` может показать несколько адресов. Выбирайте адрес интерфейса
`eth0`, `enp*` или `wlan0`, через который компьютер подключён к домашней
сети. Адреса виртуальных машин (например, VirtualBox `192.168.56.1`)
для телефона не подходят.

## 13. Полезные команды

```bash
# статус и логи
sudo systemctl status --no-pager kvhomelibrary
sudo journalctl -u kvhomelibrary -f

# перезапуск и остановка
sudo systemctl restart kvhomelibrary
sudo systemctl stop kvhomelibrary

# ручное сканирование
cd /opt/kvhomelibrary
sudo -u kvhome env PYTHONDONTWRITEBYTECODE=1 \
    python3 -m kvhomelibrary scan /srv/books \
    --db /var/lib/kvhomelibrary/library.db \
    --covers /var/lib/kvhomelibrary/covers

# статистика
sudo -u kvhome env PYTHONDONTWRITEBYTECODE=1 \
    python3 -m kvhomelibrary stats \
    --db /var/lib/kvhomelibrary/library.db

# проверка каталога
curl -I http://127.0.0.1:8080/opds
```
