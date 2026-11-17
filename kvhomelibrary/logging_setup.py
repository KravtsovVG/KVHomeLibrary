"""Настройка логирования KVHomeLibrary в файл с ротацией."""

from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
_HANDLER_MARK = "_kvhomelibrary_handler"


def default_log_path(db_path: str | Path) -> Path:
    return Path(db_path).expanduser().resolve().parent / "logs" / "kvhomelibrary.log"


def configure_logging(
    log_file: str | Path | None = None,
    *,
    level: int = logging.INFO,
    console: bool = True,
) -> Path:
    """Включить запись логов в файл и, при необходимости, в stderr.

    Возвращает путь к файлу лога. Если файл недоступен, логирование
    остаётся на stderr, а ошибка выводится в консоль.
    """
    root = logging.getLogger()
    root.setLevel(level)
    for handler in list(root.handlers):
        if getattr(handler, _HANDLER_MARK, False):
            root.removeHandler(handler)
            handler.close()

    formatter = logging.Formatter(LOG_FORMAT, DATE_FORMAT)
    path = Path(log_file) if log_file else Path("kvhomelibrary.log")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            path,
            maxBytes=5 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        )
        file_handler.setLevel(level)
        file_handler.setFormatter(formatter)
        setattr(file_handler, _HANDLER_MARK, True)
        root.addHandler(file_handler)
    except OSError as exc:
        print(f"Не удалось открыть файл логов {path}: {exc}", file=sys.stderr)

    if console:
        console_handler = logging.StreamHandler(sys.stderr)
        console_handler.setLevel(level)
        console_handler.setFormatter(formatter)
        setattr(console_handler, _HANDLER_MARK, True)
        root.addHandler(console_handler)
    return path
