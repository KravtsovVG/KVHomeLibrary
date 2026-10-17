"""Командная строка KVHomeLibrary."""

from __future__ import annotations

import argparse
import logging
import os
import socket
import sys
from pathlib import Path

from . import __version__
from .db import (
    connect_db,
    get_stats,
    init_db,
    reindex_fts,
)
from .scanner import scan_library
from .server import ServerConfig, create_server

DEFAULT_DB = os.environ.get("KVL_DB", "library.db")
DEFAULT_LIBRARY = os.environ.get("KVL_LIBRARY", "books")


def _db_parent(db: str) -> Path:
    return Path(db).expanduser().resolve().parent


def _covers_dir(db: str, covers: str | None) -> Path:
    return Path(covers).expanduser().resolve() if covers else _db_parent(db) / "covers"


def _format_size(size: int) -> str:
    value = float(size)
    for unit in ("Б", "КиБ", "МиБ", "ГиБ", "ТиБ"):
        if value < 1024 or unit == "ТиБ":
            return f"{value:.1f} {unit}" if unit != "Б" else f"{int(value)} {unit}"
        value /= 1024
    return f"{size} Б"


def _local_ipv4_addresses() -> list[str]:
    """Определить IPv4-адреса компьютера для ссылок в локальной сети."""
    addresses: list[str] = []

    # Основной адрес: UDP-соединение не отправляет пакеты, но позволяет
    # узнать IP интерфейса, через который идёт маршрут по умолчанию.
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            primary = sock.getsockname()[0]
            if primary and not primary.startswith("127."):
                addresses.append(primary)
    except OSError:
        pass

    try:
        for info in socket.getaddrinfo(
            socket.gethostname(), None, socket.AF_INET, socket.SOCK_STREAM
        ):
            ip = info[4][0]
            if ip.startswith("127.") or ip.startswith("169.254."):
                continue
            if ip not in addresses:
                addresses.append(ip)
    except OSError:
        pass

    return addresses


def _add_db_option(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--db",
        default=DEFAULT_DB,
        help=f"путь к базе SQLite (по умолчанию: {DEFAULT_DB})",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="kvhomelibrary",
        description="Домашняя библиотека FB2 с OPDS-каталогом",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init", help="создать базу и каталог обложек")
    _add_db_option(init_parser)
    init_parser.add_argument("--covers", help="каталог для обложек")
    init_parser.set_defaults(func=cmd_init)

    scan_parser = subparsers.add_parser("scan", help="просканировать каталог с FB2")
    _add_db_option(scan_parser)
    scan_parser.add_argument(
        "path",
        nargs="?",
        default=DEFAULT_LIBRARY,
        help=f"каталог с книгами (по умолчанию: {DEFAULT_LIBRARY})",
    )
    scan_parser.add_argument(
        "--no-recursive",
        action="store_true",
        help="не заходить в подкаталоги",
    )
    scan_parser.add_argument(
        "--prune",
        action="store_true",
        help="удалить из базы книги, файлы которых исчезли",
    )
    scan_parser.add_argument("--covers", help="каталог для обложек")
    scan_parser.add_argument("-q", "--quiet", action="store_true", help="без прогресса")
    scan_parser.set_defaults(func=cmd_scan)

    serve_parser = subparsers.add_parser("serve", help="запустить OPDS-сервер")
    _add_db_option(serve_parser)
    serve_parser.add_argument("--host", default="127.0.0.1", help="адрес прослушивания")
    serve_parser.add_argument("--port", type=int, default=8080, help="порт (по умолчанию 8080)")
    serve_parser.add_argument(
        "--library",
        default=DEFAULT_LIBRARY,
        help=f"каталог с книгами для --scan (по умолчанию: {DEFAULT_LIBRARY})",
    )
    serve_parser.add_argument(
        "--scan",
        action="store_true",
        help="просканировать --library перед запуском",
    )
    serve_parser.add_argument(
        "--no-recursive",
        action="store_true",
        help="при --scan не заходить в подкаталоги",
    )
    serve_parser.add_argument(
        "--prune",
        action="store_true",
        help="при --scan удалить отсутствующие файлы",
    )
    serve_parser.add_argument("--covers", help="каталог для обложек")
    serve_parser.add_argument(
        "--page-size",
        type=int,
        default=50,
        help="книг на страницу OPDS (по умолчанию 50)",
    )
    serve_parser.add_argument(
        "--public-url",
        default="",
        help="внешний URL сервера, например http://192.168.1.10:8080",
    )
    serve_parser.add_argument(
        "--admin-password",
        default=os.environ.get("KVL_ADMIN_PASSWORD", ""),
        help="пароль для /admin (иначе администрирование только с localhost)",
    )
    serve_parser.set_defaults(func=cmd_serve)

    stats_parser = subparsers.add_parser("stats", help="показать статистику библиотеки")
    _add_db_option(stats_parser)
    stats_parser.set_defaults(func=cmd_stats)

    reindex_parser = subparsers.add_parser("reindex", help="перестроить поисковый индекс")
    _add_db_option(reindex_parser)
    reindex_parser.set_defaults(func=cmd_reindex)

    return parser


def cmd_init(args: argparse.Namespace) -> int:
    db_path = init_db(args.db)
    covers = _covers_dir(args.db, args.covers)
    covers.mkdir(parents=True, exist_ok=True)
    print(f"База создана: {db_path}")
    print(f"Обложки:     {covers}")
    return 0


def _progress(index: int, total: int, path: Path, status: str) -> None:
    if total <= 50 or index % 25 == 0 or index == total:
        print(f"\r[{index}/{total}] {status:<8} {path.name[:70]}", end="", flush=True)


def cmd_scan(args: argparse.Namespace) -> int:
    library = Path(args.path).expanduser()
    if not library.is_dir():
        print(f"Ошибка: каталог не найден: {library}", file=sys.stderr)
        return 2

    covers = _covers_dir(args.db, args.covers)
    report = scan_library(
        args.db,
        library,
        recursive=not args.no_recursive,
        prune=args.prune,
        covers_dir=covers,
        progress=None if args.quiet else _progress,
    )
    if not args.quiet:
        print()

    print(f"Каталог:      {report.root}")
    print(f"Найдено файлов: {report.total}")
    print(f"Добавлено:    {report.added}")
    print(f"Обновлено:    {report.updated}")
    print(f"Без изменений: {report.skipped}")
    print(f"Ошибок:       {report.failed}")
    if args.prune:
        print(f"Удалено:      {report.removed}")
    print(f"Время:        {report.duration:.1f} с")

    if report.errors:
        print("\nОшибки разбора:", file=sys.stderr)
        for path, error in report.errors[:20]:
            print(f"  {path}: {error}", file=sys.stderr)
        if len(report.errors) > 20:
            print(f"  ... и ещё {len(report.errors) - 20}", file=sys.stderr)
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    covers = _covers_dir(args.db, args.covers)
    config = ServerConfig(
        db_path=Path(args.db).expanduser().resolve(),
        covers_dir=covers,
        page_size=max(1, args.page_size),
        public_url=args.public_url,
        library_path=Path(args.library).expanduser().resolve(),
        admin_password=args.admin_password,
    )
    init_db(config.db_path)
    config.covers_dir.mkdir(parents=True, exist_ok=True)

    if args.scan:
        library = Path(args.library).expanduser()
        if not library.is_dir():
            print(f"Ошибка: каталог не найден: {library}", file=sys.stderr)
            return 2
        print(f"Сканирование: {library}")
        report = scan_library(
            config.db_path,
            library,
            recursive=not args.no_recursive,
            prune=args.prune,
            covers_dir=config.covers_dir,
            progress=_progress,
        )
        print(
            f"\nДобавлено: {report.added}, обновлено: {report.updated}, "
            f"без изменений: {report.skipped}, ошибок: {report.failed}"
        )

    try:
        httpd = create_server(args.host, args.port, config)
    except OSError as exc:
        print(f"Не удалось запустить сервер: {exc}", file=sys.stderr)
        return 1

    port = httpd.server_address[1]
    if args.public_url:
        print(f"\nПубличный каталог: {args.public_url.rstrip('/')}/opds")
    elif args.host in {"0.0.0.0", "::", ""}:
        print(f"\nЛокально:  http://127.0.0.1:{port}/opds")
        addresses = _local_ipv4_addresses()
        if addresses:
            print(f"В сети:    http://{addresses[0]}:{port}/opds")
            for address in addresses[1:]:
                print(f"           http://{address}:{port}/opds")
            print("           (используйте адрес вашей Wi-Fi/Ethernet сети)")
        else:
            print("В сети:    не удалось определить IP — проверьте ipconfig")
            print("           запустите с --host 0.0.0.0 и укажите IP вручную")
    elif args.host in {"127.0.0.1", "localhost", "::1"}:
        print(f"\nOPDS-каталог: http://127.0.0.1:{port}/opds")
        print("Доступен только с этого компьютера.")
        print("Для доступа из локальной сети: --host 0.0.0.0")
    else:
        print(f"\nOPDS-каталог: http://{args.host}:{port}/opds")

    print(f"Веб-страница: http://127.0.0.1:{port}/")
    if args.admin_password:
        print("Администрирование защищено паролем: /admin")
    else:
        print("Администрирование: /admin (только с этого компьютера)")
    print("Остановка: Ctrl+C")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nОстановка сервера...")
    finally:
        httpd.server_close()
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    init_db(args.db)
    conn = connect_db(args.db)
    try:
        stats = get_stats(conn)
        print(f"Книг:             {stats.books}")
        print(f"Авторов:          {stats.authors}")
        print(f"Категорий:        {stats.genres}")
        print(f"Серий:            {stats.series}")
        print(f"Ошибок разбора:   {stats.failed}")
        print(f"Размер файлов:    {_format_size(stats.total_size)}")
        print(f"Последнее сканирование: {stats.last_scan or '—'}")

        if stats.failed:
            print("\nФайлы с ошибками:")
            rows = conn.execute(
                "SELECT file_path, parse_error FROM books "
                "WHERE parse_error IS NOT NULL ORDER BY updated_at DESC LIMIT 10"
            ).fetchall()
            for row in rows:
                print(f"  {row['file_path']}: {row['parse_error']}")
    finally:
        conn.close()
    return 0


def cmd_reindex(args: argparse.Namespace) -> int:
    init_db(args.db)
    conn = connect_db(args.db)
    try:
        count = reindex_fts(conn)
    finally:
        conn.close()
    if count == 0:
        print("FTS5 недоступен или в библиотеке нет книг — индекс не построен.")
    else:
        print(f"Поисковый индекс перестроен: {count} книг.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        print("\nПрервано.", file=sys.stderr)
        return 130
    except BrokenPipeError:
        return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
