#!/usr/bin/env python3
"""Привести жанры библиотеки к спецификации FictionBook 2.1.

Примеры:

    python scripts/normalize_genres_fb2.py --db data/library.db --dry-run
    python scripts/normalize_genres_fb2.py --db data/library.db
    python scripts/normalize_genres_fb2.py --list
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from kvhomelibrary.db import connect_db, init_db  # noqa: E402
from kvhomelibrary.genre_catalog import (  # noqa: E402
    list_fb2_genres,
    normalize_fb2_genres,
)
from kvhomelibrary.logging_setup import configure_logging, default_log_path  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Привести жанры к кодам и русским названиям FictionBook 2.1"
    )
    parser.add_argument("--db", default="library.db", help="путь к SQLite")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="только показать изменения, не менять базу",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="вывести канонические жанры FB2 с русскими синонимами",
    )
    parser.add_argument("--log-file", default="", help="файл логов")
    args = parser.parse_args(argv)

    if args.list:
        for code, name, synonyms in list_fb2_genres():
            suffix = " — " + ", ".join(synonyms) if synonyms else ""
            print(f"{code:<28} {name}{suffix}")
        return 0

    log_path = Path(args.log_file) if args.log_file else default_log_path(args.db)
    configure_logging(log_path)
    init_db(args.db)
    conn = connect_db(args.db)
    try:
        report = normalize_fb2_genres(conn, dry_run=args.dry_run)
    finally:
        conn.close()

    print(f"Лог:            {log_path}")
    print(f"Всего жанров:   {report.total}")
    print(f"Переименовано:  {report.renamed}")
    print(f"Объединено:     {report.merged}")
    print(f"Без изменений:  {report.unchanged}")
    print(f"Неизвестных:    {report.unknown}")
    if args.dry_run:
        print("Режим проверки: база не изменена.")
    if report.changes:
        print("\nИзменения:")
        for change in report.changes[:100]:
            action = "объединён" if change.action == "merge" else "переименован"
            print(
                f"  {change.old_code} ({change.old_name}) {action} → "
                f"{change.new_code} ({change.new_name})"
            )
        if len(report.changes) > 100:
            print(f"  ... и ещё {len(report.changes) - 100}")
    if report.unknown_genres:
        print("\nНе удалось сопоставить с FB2 2.1:", file=sys.stderr)
        for code, name in report.unknown_genres[:50]:
            print(f"  {code}: {name}", file=sys.stderr)
    if report.errors:
        print("\nОшибки:", file=sys.stderr)
        for error in report.errors:
            print(f"  {error}", file=sys.stderr)
    return 1 if report.errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
