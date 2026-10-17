#!/usr/bin/env bash
#
# Установка/обновление KVHomeLibrary из GitHub на Debian/Ubuntu.
# Запускать от root:
#   sudo bash deploy/linux/install.sh
#
# Настройки можно переопределить переменными окружения:
#   KVL_REPO_URL, KVL_BRANCH, KVL_INSTALL_DIR, KVL_DATA_DIR,
#   KVL_LIBRARY_DIR, KVL_SERVICE_USER, KVL_PYTHON, KVL_HOST,
#   KVL_PORT, KVL_ADMIN_PASSWORD, KVL_SKIP_SCAN.
#
set -euo pipefail

REPO_URL="${KVL_REPO_URL:-https://github.com/KravtsovVG/KVHomeLibrary.git}"
BRANCH="${KVL_BRANCH:-main}"
INSTALL_DIR="${KVL_INSTALL_DIR:-/opt/kvhomelibrary}"
DATA_DIR="${KVL_DATA_DIR:-/var/lib/kvhomelibrary}"
LIBRARY_DIR="${KVL_LIBRARY_DIR:-/srv/books}"
SERVICE_USER="${KVL_SERVICE_USER:-kvhome}"
PYTHON="${KVL_PYTHON:-python3}"
BIND_HOST="${KVL_HOST:-0.0.0.0}"
PORT="${KVL_PORT:-8080}"
UNIT_PATH="/etc/systemd/system/kvhomelibrary.service"
ENV_PATH="/etc/kvhomelibrary.env"

log()  { printf '\n==> %s\n' "$*"; }
warn() { printf '\n[!] %s\n' "$*" >&2; }
die()  { printf '\n[x] %s\n' "$*" >&2; exit 1; }

if [[ ${EUID:-$(id -u)} -ne 0 ]]; then
    die "Запустите скрипт от root: sudo bash $0"
fi

if [[ ! -d /run/systemd/system ]] || ! command -v systemctl >/dev/null 2>&1; then
    die "Нужен systemd: этот скрипт настраивает демон через systemd."
fi

missing=0
command -v git >/dev/null 2>&1 || missing=1
command -v "$PYTHON" >/dev/null 2>&1 || missing=1
if [[ $missing -eq 1 ]]; then
    if command -v apt-get >/dev/null 2>&1; then
        log "Устанавливаю git, Python и adduser"
        apt-get update
        DEBIAN_FRONTEND=noninteractive apt-get install -y git python3 adduser
    else
        die "Установите git и Python 3.10+ вручную (поддерживается apt)."
    fi
fi

PYTHON_BIN="$(command -v "$PYTHON" || true)"
[[ -n "$PYTHON_BIN" ]] || die "Не найден Python: $PYTHON"
"$PYTHON_BIN" - <<'PY' || die "Нужен Python 3.10 или новее."
import sys
raise SystemExit(0 if sys.version_info >= (3, 10) else 1)
PY

if ! id -u "$SERVICE_USER" >/dev/null 2>&1; then
    log "Создаю системного пользователя $SERVICE_USER"
    adduser --system --group --home "$DATA_DIR" --shell /usr/sbin/nologin \
        --no-create-home "$SERVICE_USER"
fi

log "Создаю каталоги"
install -d -m 0755 -o root -g root "$INSTALL_DIR"
install -d -m 0750 -o "$SERVICE_USER" -g "$SERVICE_USER" "$DATA_DIR"
install -d -m 0750 -o "$SERVICE_USER" -g "$SERVICE_USER" "$DATA_DIR/covers"
install -d -m 0750 -o root -g "$SERVICE_USER" "$LIBRARY_DIR"
install -d -m 0750 -o "$SERVICE_USER" -g "$SERVICE_USER" "$LIBRARY_DIR/OPDS"

IS_UPDATE=0
if [[ -d "$INSTALL_DIR/.git" ]]; then
    IS_UPDATE=1
    log "Обновляю код из GitHub"
    systemctl stop kvhomelibrary >/dev/null 2>&1 || true
    git -C "$INSTALL_DIR" fetch --depth 1 origin "$BRANCH"
    git -C "$INSTALL_DIR" reset --hard "origin/$BRANCH"
else
    if [[ -n "$(ls -A "$INSTALL_DIR" 2>/dev/null || true)" ]]; then
        die "Каталог $INSTALL_DIR не пуст и не является git-репозиторием. Освободите его или задайте KVL_INSTALL_DIR."
    fi
    log "Клонирую $REPO_URL ($BRANCH)"
    git clone --depth 1 --branch "$BRANCH" "$REPO_URL" "$INSTALL_DIR"
fi

[[ -f "$INSTALL_DIR/deploy/systemd/kvhomelibrary.service.in" ]] || \
    die "В репозитории нет deploy/systemd/kvhomelibrary.service.in"

log "Проверяю права на код"
chown -R root:root "$INSTALL_DIR"
find "$INSTALL_DIR" -type d -exec chmod 0755 {} +
find "$INSTALL_DIR" -type f -exec chmod 0644 {} +

sed_escape() {
    printf '%s' "$1" | sed -e 's/[\\&|]/\\&/g'
}

E_INSTALL="$(sed_escape "$INSTALL_DIR")"
E_DATA="$(sed_escape "$DATA_DIR")"
E_LIBRARY="$(sed_escape "$LIBRARY_DIR")"
E_USER="$(sed_escape "$SERVICE_USER")"
E_PYTHON="$(sed_escape "$PYTHON_BIN")"
E_HOST="$(sed_escape "$BIND_HOST")"
E_PORT="$(sed_escape "$PORT")"

log "Устанавливаю systemd-юнит"
sed \
    -e "s|@INSTALL_DIR@|$E_INSTALL|g" \
    -e "s|@DATA_DIR@|$E_DATA|g" \
    -e "s|@LIBRARY_DIR@|$E_LIBRARY|g" \
    -e "s|@SERVICE_USER@|$E_USER|g" \
    -e "s|@PYTHON@|$E_PYTHON|g" \
    -e "s|@HOST@|$E_HOST|g" \
    -e "s|@PORT@|$E_PORT|g" \
    "$INSTALL_DIR/deploy/systemd/kvhomelibrary.service.in" > "$UNIT_PATH"
chmod 0644 "$UNIT_PATH"

if [[ -n "${KVL_ADMIN_PASSWORD:-}" ]]; then
    log "Записываю пароль администратора в $ENV_PATH"
    (
        umask 077
        printf 'KVL_ADMIN_PASSWORD=%s\n' "$KVL_ADMIN_PASSWORD" > "$ENV_PATH"
    )
    chown root:root "$ENV_PATH"
    chmod 0600 "$ENV_PATH"
fi

run_as_service() {
    if command -v runuser >/dev/null 2>&1; then
        runuser -u "$SERVICE_USER" -- "$@"
    else
        su -s /bin/sh "$SERVICE_USER" -c "$(printf '%q ' "$@")"
    fi
}

if [[ $IS_UPDATE -eq 0 && "${KVL_SKIP_SCAN:-0}" != "1" ]]; then
    if find "$LIBRARY_DIR" -type f -iname '*.fb2' -print -quit 2>/dev/null | grep -q .; then
        log "Первое сканирование библиотеки"
        (
            cd "$INSTALL_DIR"
            run_as_service env PYTHONPATH="$INSTALL_DIR" PYTHONDONTWRITEBYTECODE=1 "$PYTHON_BIN" -m kvhomelibrary scan \
                "$LIBRARY_DIR" \
                --db "$DATA_DIR/library.db" \
                --covers "$DATA_DIR/covers"
        )
    else
        warn "В $LIBRARY_DIR нет FB2 — сканирование пропущено. Положите книги и запустите scan вручную."
    fi
fi

log "Запускаю демон"
systemctl daemon-reload
systemctl enable kvhomelibrary >/dev/null
systemctl restart kvhomelibrary

READY=0
for _ in $(seq 1 30); do
    if "$PYTHON_BIN" - "$PORT" <<'PY' >/dev/null 2>&1
import sys
import urllib.request
port = sys.argv[1]
with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=2) as response:
    raise SystemExit(0 if response.status == 200 else 1)
PY
    then
        READY=1
        break
    fi
    sleep 1
done

if [[ $READY -ne 1 ]]; then
    systemctl status --no-pager kvhomelibrary || true
    die "Сервис не ответил на /healthz. Смотрите: journalctl -u kvhomelibrary -n 80 --no-pager"
fi

IP="$(hostname -I 2>/dev/null | awk '{print $1}' || true)"
[[ -n "$IP" ]] || IP="<IP-сервера>"

cat <<EOF

Готово. KVHomeLibrary работает как systemd-демон.

  Каталог кода:  $INSTALL_DIR
  База, обложки: $DATA_DIR
  Книги:         $LIBRARY_DIR
  Сервис:        systemctl status kvhomelibrary
  Логи:          journalctl -u kvhomelibrary -f
  Веб:           http://$IP:$PORT/
  OPDS:          http://$IP:$PORT/opds

Администрирование:
  по умолчанию /admin доступен только с localhost;
  для доступа по сети задайте KVL_ADMIN_PASSWORD при установке
  или добавьте строку KVL_ADMIN_PASSWORD=... в $ENV_PATH
  и выполните: systemctl restart kvhomelibrary

Обновление:
  sudo bash $INSTALL_DIR/deploy/linux/install.sh
EOF
