"""Раскладка файлов библиотеки по каталогам.

Структура::

    <library>/<язык>/<корневая категория>/<серия>/<файл>

Если язык не указан — ``ЯзыкНеизвестен``. Если категория не указана —
``БезКатегории``. Если серия не указана, файл лежит прямо в каталоге
корневой категории.
"""

from __future__ import annotations

import re
from pathlib import Path

from .categories import category_root
from .fb2 import ParsedBook
from .genres import genre_name

UNKNOWN_LANGUAGE = "ЯзыкНеизвестен"
NO_CATEGORY = "БезКатегории"

_INVALID = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_SPACES = re.compile(r"\s+")
_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def safe_component(value: str, fallback: str) -> str:
    """Безопасное имя каталога/файла для Windows и Linux."""
    text = _INVALID.sub("_", (value or "").strip())
    text = _SPACES.sub(" ", text).strip(" .")
    if not text:
        return fallback
    if text.upper() in _RESERVED:
        text = "_" + text
    return text[:90].rstrip(" .") or fallback


def book_category(book: ParsedBook) -> str:
    for code in book.genres:
        name = genre_name(code)
        root = category_root(name)
        if root:
            return root
    return NO_CATEGORY


def book_language(book: ParsedBook) -> str:
    language = (book.lang or "").strip()
    return language or UNKNOWN_LANGUAGE


def target_path(
    library_dir: str | Path,
    book: ParsedBook,
    *,
    archive_zip: bool = False,
    original_name: str = "",
) -> Path:
    """Вычислить путь по языку/корневой категории/серии."""
    library = Path(library_dir).expanduser().resolve()
    language = safe_component(book_language(book), UNKNOWN_LANGUAGE)
    category = safe_component(book_category(book), NO_CATEGORY)
    directory = library / language / category
    series = (book.series or "").strip()
    if series:
        directory = directory / safe_component(series, "БезСерии")

    source_name = original_name or book.file_name or book.title or "book"
    base = Path(source_name.replace("\\", "/")).name
    suffix = ".zip" if archive_zip else (Path(base).suffix or ".fb2")
    stem = Path(base).stem if Path(base).suffix else base
    stem = safe_component(stem, safe_component(book.title, "book"))
    digest = (book.file_hash or "")[:10]
    filename = f"{stem}-{digest}{suffix}" if digest else f"{stem}{suffix}"
    return directory / filename


def ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
