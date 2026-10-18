"""SQLite-хранилище каталога книг.

Схема нормализована: книги, авторы, жанры и серии связаны через отдельные
таблицы. Поиск работает через FTS5, а если сборка SQLite его не поддерживает —
через обычный LIKE.
"""

from __future__ import annotations

import os
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Sequence
from urllib.parse import urlparse

from .fb2 import Fb2Author, ParsedBook
from .genres import genre_name, normalize_genre

SCHEMA = """
CREATE TABLE IF NOT EXISTS authors (
    id          INTEGER PRIMARY KEY,
    first_name  TEXT NOT NULL DEFAULT '',
    middle_name TEXT NOT NULL DEFAULT '',
    last_name   TEXT NOT NULL DEFAULT '',
    nickname    TEXT NOT NULL DEFAULT '',
    email       TEXT NOT NULL DEFAULT '',
    home_page   TEXT NOT NULL DEFAULT '',
    display_name TEXT NOT NULL,
    sort_key    TEXT NOT NULL,
    name_key    TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS genres (
    id       INTEGER PRIMARY KEY,
    code     TEXT NOT NULL UNIQUE,
    name     TEXT NOT NULL,
    sort_key TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS series (
    id      INTEGER PRIMARY KEY,
    name    TEXT NOT NULL,
    name_key TEXT NOT NULL UNIQUE,
    sort_key TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS books (
    id            INTEGER PRIMARY KEY,
    file_path     TEXT NOT NULL UNIQUE,
    file_name     TEXT NOT NULL,
    file_size     INTEGER NOT NULL DEFAULT 0,
    file_mtime    REAL NOT NULL DEFAULT 0,
    file_hash     TEXT NOT NULL UNIQUE,
    format        TEXT NOT NULL DEFAULT 'fb2',
    title         TEXT NOT NULL,
    sort_key      TEXT NOT NULL,
    annotation    TEXT NOT NULL DEFAULT '',
    keywords      TEXT NOT NULL DEFAULT '',
    lang          TEXT NOT NULL DEFAULT '',
    src_lang      TEXT NOT NULL DEFAULT '',
    book_date     TEXT NOT NULL DEFAULT '',
    publisher     TEXT NOT NULL DEFAULT '',
    city          TEXT NOT NULL DEFAULT '',
    publish_year  TEXT NOT NULL DEFAULT '',
    isbn          TEXT NOT NULL DEFAULT '',
    doc_id        TEXT NOT NULL DEFAULT '',
    doc_version   TEXT NOT NULL DEFAULT '',
    doc_date      TEXT NOT NULL DEFAULT '',
    translators   TEXT NOT NULL DEFAULT '',
    series_id     INTEGER REFERENCES series(id) ON DELETE SET NULL,
    series_number TEXT NOT NULL DEFAULT '',
    cover_path    TEXT,
    cover_mime    TEXT,
    parse_error   TEXT,
    added_at      TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS book_authors (
    book_id   INTEGER NOT NULL REFERENCES books(id) ON DELETE CASCADE,
    author_id INTEGER NOT NULL REFERENCES authors(id) ON DELETE CASCADE,
    position  INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (book_id, author_id)
);

CREATE TABLE IF NOT EXISTS book_genres (
    book_id  INTEGER NOT NULL REFERENCES books(id) ON DELETE CASCADE,
    genre_id INTEGER NOT NULL REFERENCES genres(id) ON DELETE CASCADE,
    PRIMARY KEY (book_id, genre_id)
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS opds_sources (
    id              INTEGER PRIMARY KEY,
    name            TEXT NOT NULL,
    url             TEXT NOT NULL UNIQUE,
    username        TEXT NOT NULL DEFAULT '',
    password        TEXT NOT NULL DEFAULT '',
    enabled         INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    last_checked_at TEXT NOT NULL DEFAULT '',
    last_error      TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_books_sort ON books(sort_key);
CREATE INDEX IF NOT EXISTS idx_books_series ON books(series_id);
CREATE INDEX IF NOT EXISTS idx_books_updated ON books(updated_at);
CREATE INDEX IF NOT EXISTS idx_book_authors_author ON book_authors(author_id);
CREATE INDEX IF NOT EXISTS idx_book_genres_genre ON book_genres(genre_id);
CREATE INDEX IF NOT EXISTS idx_series_sort ON series(sort_key);
CREATE INDEX IF NOT EXISTS idx_genres_sort ON genres(sort_key);
CREATE INDEX IF NOT EXISTS idx_authors_sort ON authors(sort_key);
CREATE INDEX IF NOT EXISTS idx_opds_sources_name ON opds_sources(name);
"""

FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS books_fts USING fts5(
    book_id UNINDEXED,
    title,
    annotation,
    authors,
    series,
    tokenize='unicode61'
);
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _key(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "").casefold()).strip()


def _sort_key(value: str) -> str:
    value = _key(value)
    value = re.sub(r"^(?:the|a|an)\s+", "", value)
    return re.sub(r"[«»\"'`]", "", value).strip()


def connect_db(path: str | Path) -> sqlite3.Connection:
    """Открыть соединение с БД (по одному на запрос/операцию)."""
    db_path = Path(path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    try:
        conn.execute("PRAGMA journal_mode = WAL")
    except sqlite3.DatabaseError:
        # На сетевых дисках и в некоторых ФС WAL недоступен — работаем в
        # обычном режиме журнала.
        pass
    return conn


def init_db(path: str | Path) -> Path:
    """Создать схему БД и включить FTS, если он доступен."""
    db_path = Path(path)
    conn = connect_db(db_path)
    try:
        conn.executescript(SCHEMA)
        columns = {
            str(row["name"])
            for row in conn.execute("PRAGMA table_info(books)").fetchall()
        }
        if "format" not in columns:
            conn.execute(
                "ALTER TABLE books ADD COLUMN format TEXT NOT NULL DEFAULT 'fb2'"
            )
        try:
            conn.executescript(FTS_SCHEMA)
        except sqlite3.OperationalError:
            pass
        normalize_genres(conn)
        conn.commit()
    finally:
        conn.close()
    return db_path


def has_fts(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='books_fts'"
    ).fetchone()
    return row is not None


@dataclass(slots=True)
class AuthorRef:
    id: int
    name: str
    sort_key: str = ""
    books: int = 0


@dataclass(slots=True)
class GenreRef:
    id: int
    code: str
    name: str
    sort_key: str = ""
    books: int = 0


@dataclass(slots=True)
class SeriesRef:
    id: int
    name: str
    sort_key: str = ""
    books: int = 0


@dataclass(slots=True)
class BookRef:
    id: int
    title: str
    annotation: str = ""
    lang: str = ""
    year: str = ""
    publisher: str = ""
    city: str = ""
    book_date: str = ""
    isbn: str = ""
    keywords: str = ""
    translators: str = ""
    doc_id: str = ""
    series_id: int | None = None
    series_name: str = ""
    series_number: str = ""
    cover_path: str | None = None
    cover_mime: str = ""
    file_name: str = ""
    file_path: str = ""
    file_size: int = 0
    format: str = "fb2"
    added_at: str = ""
    updated_at: str = ""
    authors: list[AuthorRef] = field(default_factory=list)
    genres: list[GenreRef] = field(default_factory=list)

    @property
    def is_zip(self) -> bool:
        return self.file_name.lower().endswith(".zip")


@dataclass(slots=True)
class Stats:
    books: int = 0
    authors: int = 0
    genres: int = 0
    series: int = 0
    failed: int = 0
    total_size: int = 0
    last_scan: str = ""


@dataclass(slots=True)
class OpdsSource:
    id: int
    name: str
    url: str
    username: str = ""
    password: str = ""
    enabled: bool = True
    created_at: str = ""
    updated_at: str = ""
    last_checked_at: str = ""
    last_error: str = ""


@dataclass(slots=True)
class ScanOutcome:
    added: int = 0
    updated: int = 0
    skipped: int = 0
    failed: int = 0
    removed: int = 0
    total: int = 0


def _save_cover(book: ParsedBook, covers_dir: Path | None) -> tuple[str | None, str]:
    if not book.cover_data or covers_dir is None:
        return None, ""
    covers_dir.mkdir(parents=True, exist_ok=True)
    ext = book.cover_ext or "jpg"
    filename = f"{book.file_hash[:24]}.{ext}"
    target = covers_dir / filename
    if not target.exists():
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_bytes(book.cover_data)
        os.replace(tmp, target)
    return filename, book.cover_mime


def _get_or_create_author(conn: sqlite3.Connection, author: Fb2Author) -> int:
    display = author.display_name
    name_key = _key(display) or "неизвестный автор"
    sort_key = _sort_key(author.sort_name) or name_key

    row = conn.execute(
        "SELECT id FROM authors WHERE name_key = ?", (name_key,)
    ).fetchone()
    if row is not None:
        return int(row["id"])

    cur = conn.execute(
        """
        INSERT INTO authors(first_name, middle_name, last_name, nickname,
                            email, home_page, display_name, sort_key, name_key)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            author.first_name,
            author.middle_name,
            author.last_name,
            author.nickname,
            author.email,
            author.home_page,
            display,
            sort_key,
            name_key,
        ),
    )
    return int(cur.lastrowid)


def _get_or_create_genre(conn: sqlite3.Connection, code: str) -> int:
    code = normalize_genre(code)
    name = genre_name(code)
    key = _sort_key(name) or _key(code)
    conn.execute(
        """
        INSERT INTO genres(code, name, sort_key) VALUES (?, ?, ?)
        ON CONFLICT(code) DO UPDATE SET name = excluded.name, sort_key = excluded.sort_key
        """,
        (code, name, key),
    )
    row = conn.execute("SELECT id FROM genres WHERE code = ?", (code,)).fetchone()
    assert row is not None
    return int(row["id"])


def normalize_genres(conn: sqlite3.Connection) -> int:
    """Слить варианты кодов жанров и проставить русские названия.

    В старых библиотеках встречаются ``sf-action``, ``sf_action`` и
    ``Боевая фантастика`` как три отдельные категории. Функция приводит их
    к одному каноническому коду и переносит связи книг.
    """
    rows = conn.execute("SELECT id, code FROM genres ORDER BY id").fetchall()
    changed = 0
    with conn:
        for row in rows:
            old_id = int(row["id"])
            old_code = str(row["code"] or "").strip()
            canonical = normalize_genre(old_code)
            name = genre_name(canonical)
            sort_key = _sort_key(name) or _key(canonical)

            target = conn.execute(
                "SELECT id FROM genres WHERE code = ?", (canonical,)
            ).fetchone()
            if target is not None and int(target["id"]) != old_id:
                target_id = int(target["id"])
                conn.execute(
                    "INSERT OR IGNORE INTO book_genres(book_id, genre_id) "
                    "SELECT book_id, ? FROM book_genres WHERE genre_id = ?",
                    (target_id, old_id),
                )
                conn.execute("DELETE FROM book_genres WHERE genre_id = ?", (old_id,))
                conn.execute("DELETE FROM genres WHERE id = ?", (old_id,))
                conn.execute(
                    "UPDATE genres SET name = ?, sort_key = ? WHERE id = ?",
                    (name, sort_key, target_id),
                )
                changed += 1
            else:
                if canonical != old_code:
                    changed += 1
                conn.execute(
                    "UPDATE genres SET code = ?, name = ?, sort_key = ? WHERE id = ?",
                    (canonical, name, sort_key, old_id),
                )
    return changed


def _get_or_create_series(conn: sqlite3.Connection, name: str) -> int | None:
    name = (name or "").strip()
    if not name:
        return None
    name_key = _key(name)
    sort_key = _sort_key(name)
    conn.execute(
        """
        INSERT INTO series(name, name_key, sort_key) VALUES (?, ?, ?)
        ON CONFLICT(name_key) DO UPDATE SET name = excluded.name, sort_key = excluded.sort_key
        """,
        (name, name_key, sort_key),
    )
    row = conn.execute("SELECT id FROM series WHERE name_key = ?", (name_key,)).fetchone()
    assert row is not None
    return int(row["id"])


def _fts_values(book: ParsedBook, author_names: Sequence[str]) -> tuple[str, str]:
    return ", ".join(author_names), book.series or ""


def _fts_insert(conn: sqlite3.Connection, book_id: int, book: ParsedBook, author_names: Sequence[str]) -> None:
    if not has_fts(conn):
        return
    authors, series = _fts_values(book, author_names)
    content = "\n".join(part for part in (book.annotation, book.keywords) if part)
    conn.execute("DELETE FROM books_fts WHERE book_id = ?", (book_id,))
    conn.execute(
        "INSERT INTO books_fts(book_id, title, annotation, authors, series) VALUES (?, ?, ?, ?, ?)",
        (book_id, book.title, content, authors, series),
    )


def upsert_book(
    conn: sqlite3.Connection,
    book: ParsedBook,
    *,
    mtime: float = 0.0,
    covers_dir: str | Path | None = None,
) -> tuple[int, str]:
    """Добавить или обновить книгу. Возвращает (id, 'added'|'updated'|'duplicate')."""
    covers = Path(covers_dir) if covers_dir else None
    file_path = str(Path(book.source_path).resolve())
    now = utc_now()

    with conn:
        by_hash = conn.execute(
            "SELECT id, file_path FROM books WHERE file_hash = ?", (book.file_hash,)
        ).fetchone()
        by_path = conn.execute(
            "SELECT id, file_hash FROM books WHERE file_path = ?", (file_path,)
        ).fetchone()

        if by_path is not None and by_hash is not None and by_path["id"] != by_hash["id"]:
            # Тот же путь занят другой версией файла, а новая версия уже
            # импортирована под другим путём — удаляем устаревшую запись.
            delete_books(conn, [int(by_path["id"])])
            by_path = None

        if by_hash is not None and by_path is None:
            old_path = Path(str(by_hash["file_path"]))
            if old_path.exists() and os.path.normcase(
                os.path.abspath(str(old_path))
            ) != os.path.normcase(os.path.abspath(file_path)):
                # Это копия уже импортированной книги. Оставляем в каталоге
                # исходный путь, чтобы повторное сканирование не «переставляло»
                # одну и ту же книгу между копиями.
                return int(by_hash["id"]), "duplicate"

        target = by_path or by_hash
        status = "updated" if target is not None else "added"

        cover_path, cover_mime = _save_cover(book, covers)
        authors = list(book.authors)
        if not authors:
            authors = [Fb2Author(nickname="Неизвестный автор")]

        author_ids: list[tuple[int, int]] = []
        for position, author in enumerate(authors):
            author_ids.append((_get_or_create_author(conn, author), position))

        series_id = _get_or_create_series(conn, book.series)
        genre_ids = [_get_or_create_genre(conn, code) for code in book.genres]

        values = (
            file_path,
            book.file_name,
            book.file_size,
            mtime,
            book.file_hash,
            book.format or "fb2",
            book.title,
            book.sort_key,
            book.annotation,
            book.keywords,
            book.lang,
            book.src_lang,
            book.book_date,
            book.publisher,
            book.city,
            book.publish_year,
            book.isbn,
            book.doc_id,
            book.doc_version,
            book.doc_date,
            book.translators,
            series_id,
            book.series_number,
            cover_path,
            cover_mime,
            now,
        )

        if target is None:
            cur = conn.execute(
                """
                INSERT INTO books(
                    file_path, file_name, file_size, file_mtime, file_hash, format,
                    title, sort_key, annotation, keywords, lang, src_lang,
                    book_date, publisher, city, publish_year, isbn, doc_id,
                    doc_version, doc_date, translators, series_id, series_number,
                    cover_path, cover_mime, added_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (*values[:-1], now, now),
            )
            book_id = int(cur.lastrowid)
        else:
            book_id = int(target["id"])
            conn.execute(
                """
                UPDATE books SET
                    file_path = ?, file_name = ?, file_size = ?, file_mtime = ?,
                    file_hash = ?, format = ?, title = ?, sort_key = ?, annotation = ?,
                    keywords = ?, lang = ?, src_lang = ?, book_date = ?,
                    publisher = ?, city = ?, publish_year = ?, isbn = ?,
                    doc_id = ?, doc_version = ?, doc_date = ?, translators = ?,
                    series_id = ?, series_number = ?, cover_path = ?, cover_mime = ?,
                    parse_error = NULL, updated_at = ?
                WHERE id = ?
                """,
                (*values, book_id),
            )

        conn.execute("DELETE FROM book_authors WHERE book_id = ?", (book_id,))
        conn.execute("DELETE FROM book_genres WHERE book_id = ?", (book_id,))
        conn.executemany(
            "INSERT OR IGNORE INTO book_authors(book_id, author_id, position) VALUES (?, ?, ?)",
            [(book_id, author_id, position) for author_id, position in author_ids],
        )
        conn.executemany(
            "INSERT OR IGNORE INTO book_genres(book_id, genre_id) VALUES (?, ?)",
            [(book_id, genre_id) for genre_id in genre_ids],
        )
        _fts_insert(conn, book_id, book, [author.display_name for author in authors])

    return book_id, status


def record_parse_error(
    conn: sqlite3.Connection,
    path: str | Path,
    error: str,
    *,
    file_hash: str,
    file_size: int,
    mtime: float,
) -> int:
    """Сохранить книгу с ошибкой разбора, чтобы она была видна в статистике."""
    file_path = str(Path(path).resolve())
    title = Path(path).stem
    now = utc_now()
    with conn:
        row = conn.execute(
            "SELECT id FROM books WHERE file_path = ? OR file_hash = ?",
            (file_path, file_hash),
        ).fetchone()
        if row is not None:
            book_id = int(row["id"])
            conn.execute(
                """
                UPDATE books SET file_path=?, file_name=?, file_size=?, file_mtime=?,
                    file_hash=?, title=?, parse_error=?, updated_at=?
                WHERE id=?
                """,
                (
                    file_path,
                    Path(path).name,
                    file_size,
                    mtime,
                    file_hash,
                    title,
                    error[:2000],
                    now,
                    book_id,
                ),
            )
            conn.execute("DELETE FROM book_authors WHERE book_id = ?", (book_id,))
            conn.execute("DELETE FROM book_genres WHERE book_id = ?", (book_id,))
            if has_fts(conn):
                conn.execute("DELETE FROM books_fts WHERE book_id = ?", (book_id,))
        else:
            cur = conn.execute(
                """
                INSERT INTO books(file_path, file_name, file_size, file_mtime,
                                  file_hash, title, sort_key, parse_error,
                                  added_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    file_path,
                    Path(path).name,
                    file_size,
                    mtime,
                    file_hash,
                    title,
                    _sort_key(title),
                    error[:2000],
                    now,
                    now,
                ),
            )
            book_id = int(cur.lastrowid)
    return book_id


def delete_books(conn: sqlite3.Connection, ids: Iterable[int]) -> int:
    """Удалить книги по id вместе с записями FTS."""
    book_ids = [int(item) for item in ids]
    if not book_ids:
        return 0
    deleted = 0
    with conn:
        for start in range(0, len(book_ids), 500):
            chunk = book_ids[start : start + 500]
            placeholders = ",".join("?" for _ in chunk)
            if has_fts(conn):
                conn.execute(
                    f"DELETE FROM books_fts WHERE book_id IN ({placeholders})", chunk
                )
            cur = conn.execute(
                f"DELETE FROM books WHERE id IN ({placeholders})", chunk
            )
            deleted += cur.rowcount
    return deleted


def delete_missing(conn: sqlite3.Connection, seen_paths: Iterable[str | Path]) -> int:
    """Удалить из БД книги, файлов которых больше нет на диске."""
    seen = {os.path.normcase(os.path.abspath(str(path))) for path in seen_paths}
    missing = [
        int(row["id"])
        for row in conn.execute("SELECT id, file_path FROM books")
        if os.path.normcase(os.path.abspath(row["file_path"])) not in seen
    ]
    return delete_books(conn, missing)


def get_meta(conn: sqlite3.Connection, key: str) -> str:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return str(row["value"]) if row is not None else ""


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    with conn:
        conn.execute(
            "INSERT INTO meta(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


def _book_from_row(conn: sqlite3.Connection, row: sqlite3.Row) -> BookRef:
    authors = [
        AuthorRef(id=int(item["id"]), name=str(item["display_name"]))
        for item in conn.execute(
            """
            SELECT a.id, a.display_name
            FROM authors a JOIN book_authors ba ON ba.author_id = a.id
            WHERE ba.book_id = ? ORDER BY ba.position, a.display_name
            """,
            (row["id"],),
        )
    ]
    genres = [
        GenreRef(id=int(item["id"]), code=str(item["code"]), name=str(item["name"]))
        for item in conn.execute(
            """
            SELECT g.id, g.code, g.name
            FROM genres g JOIN book_genres bg ON bg.genre_id = g.id
            WHERE bg.book_id = ? ORDER BY g.sort_key, g.name
            """,
            (row["id"],),
        )
    ]
    year = str(row["publish_year"] or "")
    if not year and row["book_date"]:
        match = re.match(r"\d{4}", str(row["book_date"]))
        if match:
            year = match.group(0)
    return BookRef(
        id=int(row["id"]),
        title=str(row["title"]),
        annotation=str(row["annotation"] or ""),
        lang=str(row["lang"] or ""),
        year=year,
        publisher=str(row["publisher"] or ""),
        city=str(row["city"] or ""),
        book_date=str(row["book_date"] or ""),
        isbn=str(row["isbn"] or ""),
        keywords=str(row["keywords"] or ""),
        translators=str(row["translators"] or ""),
        doc_id=str(row["doc_id"] or ""),
        series_id=int(row["series_id"]) if row["series_id"] is not None else None,
        series_name=str(row["series_name"] or ""),
        series_number=str(row["series_number"] or ""),
        cover_path=str(row["cover_path"]) if row["cover_path"] else None,
        cover_mime=str(row["cover_mime"] or ""),
        file_name=str(row["file_name"] or ""),
        file_path=str(row["file_path"] or ""),
        file_size=int(row["file_size"] or 0),
        format=str(row["format"] or "fb2"),
        added_at=str(row["added_at"] or ""),
        updated_at=str(row["updated_at"] or ""),
        authors=authors,
        genres=genres,
    )


def get_book(conn: sqlite3.Connection, book_id: int) -> BookRef | None:
    row = conn.execute(
        "SELECT b.*, s.name AS series_name FROM books b "
        "LEFT JOIN series s ON s.id = b.series_id WHERE b.id = ?",
        (book_id,),
    ).fetchone()
    if row is None:
        return None
    return _book_from_row(conn, row)


def refresh_book_fts(conn: sqlite3.Connection, book_id: int) -> None:
    """Обновить FTS-запись книги по её текущим данным в БД."""
    if not has_fts(conn):
        return
    row = conn.execute(
        "SELECT b.title, b.annotation, b.keywords, s.name AS series_name "
        "FROM books b LEFT JOIN series s ON s.id = b.series_id WHERE b.id = ?",
        (book_id,),
    ).fetchone()
    if row is None:
        return
    authors = [
        str(item["display_name"])
        for item in conn.execute(
            "SELECT a.display_name FROM authors a "
            "JOIN book_authors ba ON ba.author_id = a.id "
            "WHERE ba.book_id = ? ORDER BY ba.position",
            (book_id,),
        )
    ]
    content = "\n".join(
        part for part in (row["annotation"], row["keywords"]) if part
    )
    conn.execute("DELETE FROM books_fts WHERE book_id = ?", (book_id,))
    conn.execute(
        "INSERT INTO books_fts(book_id, title, annotation, authors, series) "
        "VALUES (?, ?, ?, ?, ?)",
        (
            book_id,
            row["title"],
            content,
            ", ".join(authors),
            row["series_name"] or "",
        ),
    )


def update_book_metadata(
    conn: sqlite3.Connection,
    book_id: int,
    *,
    title: str,
    annotation: str = "",
    lang: str = "",
    publisher: str = "",
    city: str = "",
    publish_year: str = "",
    isbn: str = "",
    series: str = "",
    series_number: str = "",
    authors: Sequence[str] = (),
    genres: Sequence[str] = (),
) -> bool:
    """Изменить метаданные книги в БД (без изменения FB2-файла)."""
    title = (title or "").strip()
    if not title:
        return False
    if conn.execute("SELECT 1 FROM books WHERE id = ?", (book_id,)).fetchone() is None:
        return False

    author_names: list[str] = []
    seen_names: set[str] = set()
    for name in authors:
        name = re.sub(r"\s+", " ", (name or "")).strip()
        key = name.casefold()
        if name and key not in seen_names:
            seen_names.add(key)
            author_names.append(name)

    genre_codes: list[str] = []
    seen_codes: set[str] = set()
    for code in genres:
        code = (code or "").strip()
        key = code.casefold()
        if code and key not in seen_codes:
            seen_codes.add(key)
            genre_codes.append(code)

    series = (series or "").strip()
    series_number = (series_number or "").strip()
    now = utc_now()

    with conn:
        series_id = _get_or_create_series(conn, series)
        author_ids = [
            _get_or_create_author(conn, Fb2Author(nickname=name))
            for name in author_names
        ]
        if not author_ids:
            author_ids = [
                _get_or_create_author(conn, Fb2Author(nickname="Неизвестный автор"))
            ]
        genre_ids = [_get_or_create_genre(conn, code) for code in genre_codes]

        conn.execute(
            """
            UPDATE books SET
                title = ?, sort_key = ?, annotation = ?, lang = ?,
                publisher = ?, city = ?, publish_year = ?, isbn = ?,
                series_id = ?, series_number = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                title,
                _sort_key(title),
                (annotation or "").strip(),
                (lang or "").strip(),
                (publisher or "").strip(),
                (city or "").strip(),
                (publish_year or "").strip(),
                (isbn or "").strip(),
                series_id,
                series_number,
                now,
                book_id,
            ),
        )
        conn.execute("DELETE FROM book_authors WHERE book_id = ?", (book_id,))
        conn.execute("DELETE FROM book_genres WHERE book_id = ?", (book_id,))
        conn.executemany(
            "INSERT INTO book_authors(book_id, author_id, position) VALUES (?, ?, ?)",
            [(book_id, author_id, position) for position, author_id in enumerate(author_ids)],
        )
        conn.executemany(
            "INSERT OR IGNORE INTO book_genres(book_id, genre_id) VALUES (?, ?)",
            [(book_id, genre_id) for genre_id in genre_ids],
        )
        refresh_book_fts(conn, book_id)
    return True


def list_parse_errors(conn: sqlite3.Connection, *, limit: int = 20) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT id, file_path, parse_error, updated_at FROM books "
        "WHERE parse_error IS NOT NULL ORDER BY updated_at DESC LIMIT ?",
        (limit,),
    ).fetchall()


def _like_pattern(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _fts_query(value: str) -> str:
    tokens = re.findall(r"\w+", value, flags=re.UNICODE)
    return " ".join(f'"{token}"*' for token in tokens[:12])


def list_books(
    conn: sqlite3.Connection,
    *,
    author_id: int | None = None,
    genre_id: int | None = None,
    series_id: int | None = None,
    query: str | None = None,
    offset: int = 0,
    limit: int = 50,
    sort: str = "title",
    include_errors: bool = False,
) -> tuple[list[BookRef], int]:
    """Вернуть страницу книг и общее количество по фильтру."""
    where: list[str] = []
    params: list[object] = []

    if not include_errors:
        where.append("b.parse_error IS NULL")
    if author_id is not None:
        where.append(
            "EXISTS (SELECT 1 FROM book_authors ba WHERE ba.book_id = b.id AND ba.author_id = ?)"
        )
        params.append(author_id)
    if genre_id is not None:
        where.append(
            "EXISTS (SELECT 1 FROM book_genres bg WHERE bg.book_id = b.id AND bg.genre_id = ?)"
        )
        params.append(genre_id)
    if series_id is not None:
        where.append("b.series_id = ?")
        params.append(series_id)

    query = (query or "").strip()
    use_fts = False
    match_query = ""
    if query and has_fts(conn):
        match_query = _fts_query(query)
        use_fts = bool(match_query)

    if query and not use_fts:
        pattern = _like_pattern(query)
        where.append(
            "("
            "b.title LIKE ? ESCAPE '\\' OR b.annotation LIKE ? ESCAPE '\\' "
            "OR b.keywords LIKE ? ESCAPE '\\' "
            "OR s.name LIKE ? ESCAPE '\\' "
            "OR EXISTS (SELECT 1 FROM book_authors ba JOIN authors a ON a.id = ba.author_id "
            "WHERE ba.book_id = b.id AND a.display_name LIKE ? ESCAPE '\\')"
            ")"
        )
        params.extend([pattern, pattern, pattern, pattern, pattern])

    if use_fts:
        where.append("books_fts MATCH ?")
        params.append(match_query)

    where_sql = (" WHERE " + " AND ".join(where)) if where else ""
    join_fts = " JOIN books_fts ON books_fts.book_id = b.id" if use_fts else ""
    base = (
        "FROM books b "
        "LEFT JOIN series s ON s.id = b.series_id"
        f"{join_fts}{where_sql}"
    )

    total_row = conn.execute(f"SELECT COUNT(*) AS n {base}", params).fetchone()
    total = int(total_row["n"]) if total_row is not None else 0

    if use_fts:
        order = "ORDER BY rank, b.sort_key, b.id"
    elif sort == "new":
        order = "ORDER BY b.updated_at DESC, b.id DESC"
    elif series_id is not None or sort == "series":
        order = (
            "ORDER BY (b.series_number = '') ASC, "
            "CAST(b.series_number AS REAL) ASC, b.series_number, b.sort_key, b.id"
        )
    else:
        order = "ORDER BY b.sort_key, b.id"

    rows = conn.execute(
        f"SELECT b.*, s.name AS series_name {base} {order} LIMIT ? OFFSET ?",
        [*params, limit, offset],
    ).fetchall()
    return [_book_from_row(conn, row) for row in rows], total


def list_authors(conn: sqlite3.Connection, *, offset: int = 0, limit: int = 100) -> tuple[list[AuthorRef], int]:
    total = int(
        conn.execute(
            "SELECT COUNT(DISTINCT a.id) AS n FROM authors a "
            "JOIN book_authors ba ON ba.author_id = a.id "
            "JOIN books b ON b.id = ba.book_id WHERE b.parse_error IS NULL"
        ).fetchone()["n"]
    )
    rows = conn.execute(
        """
        SELECT a.id, a.display_name, a.sort_key, COUNT(DISTINCT b.id) AS books
        FROM authors a
        JOIN book_authors ba ON ba.author_id = a.id
        JOIN books b ON b.id = ba.book_id
        WHERE b.parse_error IS NULL
        GROUP BY a.id
        ORDER BY a.sort_key, a.display_name, a.id
        LIMIT ? OFFSET ?
        """,
        (limit, offset),
    ).fetchall()
    return (
        [
            AuthorRef(
                id=int(row["id"]),
                name=str(row["display_name"]),
                sort_key=str(row["sort_key"]),
                books=int(row["books"]),
            )
            for row in rows
        ],
        total,
    )


def get_author(conn: sqlite3.Connection, author_id: int) -> AuthorRef | None:
    row = conn.execute(
        """
        SELECT a.id, a.display_name, a.sort_key, COUNT(DISTINCT b.id) AS books
        FROM authors a
        LEFT JOIN book_authors ba ON ba.author_id = a.id
        LEFT JOIN books b ON b.id = ba.book_id AND b.parse_error IS NULL
        WHERE a.id = ? GROUP BY a.id
        """,
        (author_id,),
    ).fetchone()
    if row is None:
        return None
    return AuthorRef(
        id=int(row["id"]),
        name=str(row["display_name"]),
        sort_key=str(row["sort_key"]),
        books=int(row["books"]),
    )


def list_genres(conn: sqlite3.Connection, *, offset: int = 0, limit: int = 100) -> tuple[list[GenreRef], int]:
    total = int(
        conn.execute(
            "SELECT COUNT(DISTINCT g.id) AS n FROM genres g "
            "JOIN book_genres bg ON bg.genre_id = g.id "
            "JOIN books b ON b.id = bg.book_id WHERE b.parse_error IS NULL"
        ).fetchone()["n"]
    )
    rows = conn.execute(
        """
        SELECT g.id, g.code, g.name, g.sort_key, COUNT(DISTINCT b.id) AS books
        FROM genres g
        JOIN book_genres bg ON bg.genre_id = g.id
        JOIN books b ON b.id = bg.book_id
        WHERE b.parse_error IS NULL
        GROUP BY g.id
        ORDER BY g.sort_key, g.name, g.id
        LIMIT ? OFFSET ?
        """,
        (limit, offset),
    ).fetchall()
    return (
        [
            GenreRef(
                id=int(row["id"]),
                code=str(row["code"]),
                name=str(row["name"]),
                sort_key=str(row["sort_key"]),
                books=int(row["books"]),
            )
            for row in rows
        ],
        total,
    )


def get_genre(conn: sqlite3.Connection, genre_id: int) -> GenreRef | None:
    row = conn.execute(
        """
        SELECT g.id, g.code, g.name, g.sort_key, COUNT(DISTINCT b.id) AS books
        FROM genres g
        LEFT JOIN book_genres bg ON bg.genre_id = g.id
        LEFT JOIN books b ON b.id = bg.book_id AND b.parse_error IS NULL
        WHERE g.id = ? GROUP BY g.id
        """,
        (genre_id,),
    ).fetchone()
    if row is None:
        return None
    return GenreRef(
        id=int(row["id"]),
        code=str(row["code"]),
        name=str(row["name"]),
        sort_key=str(row["sort_key"]),
        books=int(row["books"]),
    )


def list_series(conn: sqlite3.Connection, *, offset: int = 0, limit: int = 100) -> tuple[list[SeriesRef], int]:
    total = int(
        conn.execute(
            "SELECT COUNT(DISTINCT s.id) AS n FROM series s "
            "JOIN books b ON b.series_id = s.id WHERE b.parse_error IS NULL"
        ).fetchone()["n"]
    )
    rows = conn.execute(
        """
        SELECT s.id, s.name, s.sort_key, COUNT(DISTINCT b.id) AS books
        FROM series s
        JOIN books b ON b.series_id = s.id
        WHERE b.parse_error IS NULL
        GROUP BY s.id
        ORDER BY s.sort_key, s.name, s.id
        LIMIT ? OFFSET ?
        """,
        (limit, offset),
    ).fetchall()
    return (
        [
            SeriesRef(
                id=int(row["id"]),
                name=str(row["name"]),
                sort_key=str(row["sort_key"]),
                books=int(row["books"]),
            )
            for row in rows
        ],
        total,
    )


def get_series(conn: sqlite3.Connection, series_id: int) -> SeriesRef | None:
    row = conn.execute(
        """
        SELECT s.id, s.name, s.sort_key, COUNT(DISTINCT b.id) AS books
        FROM series s
        LEFT JOIN books b ON b.series_id = s.id AND b.parse_error IS NULL
        WHERE s.id = ? GROUP BY s.id
        """,
        (series_id,),
    ).fetchone()
    if row is None:
        return None
    return SeriesRef(
        id=int(row["id"]),
        name=str(row["name"]),
        sort_key=str(row["sort_key"]),
        books=int(row["books"]),
    )


def get_stats(conn: sqlite3.Connection) -> Stats:
    books = int(
        conn.execute("SELECT COUNT(*) AS n FROM books WHERE parse_error IS NULL").fetchone()["n"]
    )
    failed = int(
        conn.execute("SELECT COUNT(*) AS n FROM books WHERE parse_error IS NOT NULL").fetchone()["n"]
    )
    size = int(
        conn.execute(
            "SELECT COALESCE(SUM(file_size), 0) AS n FROM books WHERE parse_error IS NULL"
        ).fetchone()["n"]
    )
    return Stats(
        books=books,
        authors=count_authors(conn),
        genres=count_genres(conn),
        series=count_series(conn),
        failed=failed,
        total_size=size,
        last_scan=get_meta(conn, "last_scan"),
    )


def _opds_source_from_row(row: sqlite3.Row) -> OpdsSource:
    return OpdsSource(
        id=int(row["id"]),
        name=str(row["name"]),
        url=str(row["url"]),
        username=str(row["username"] or ""),
        password=str(row["password"] or ""),
        enabled=bool(row["enabled"]),
        created_at=str(row["created_at"] or ""),
        updated_at=str(row["updated_at"] or ""),
        last_checked_at=str(row["last_checked_at"] or ""),
        last_error=str(row["last_error"] or ""),
    )


def _validate_opds_source(name: str, url: str) -> tuple[str, str]:
    name = (name or "").strip()
    url = (url or "").strip()
    if not name:
        raise ValueError("название источника не может быть пустым")
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("адрес источника должен начинаться с http:// или https://")
    return name, url


def list_opds_sources(conn: sqlite3.Connection, *, enabled_only: bool = False) -> list[OpdsSource]:
    sql = "SELECT * FROM opds_sources"
    if enabled_only:
        sql += " WHERE enabled = 1"
    sql += " ORDER BY name COLLATE NOCASE, id"
    return [_opds_source_from_row(row) for row in conn.execute(sql)]


def get_opds_source(conn: sqlite3.Connection, source_id: int) -> OpdsSource | None:
    row = conn.execute("SELECT * FROM opds_sources WHERE id = ?", (source_id,)).fetchone()
    return _opds_source_from_row(row) if row is not None else None


def create_opds_source(
    conn: sqlite3.Connection,
    *,
    name: str,
    url: str,
    username: str = "",
    password: str = "",
    enabled: bool = True,
) -> int:
    name, url = _validate_opds_source(name, url)
    now = utc_now()
    with conn:
        cur = conn.execute(
            """
            INSERT INTO opds_sources(name, url, username, password, enabled,
                                     created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (name, url, (username or "").strip(), password or "", 1 if enabled else 0, now, now),
        )
    return int(cur.lastrowid)


def update_opds_source(
    conn: sqlite3.Connection,
    source_id: int,
    *,
    name: str | None = None,
    url: str | None = None,
    username: str | None = None,
    password: str | None = None,
    enabled: bool | None = None,
) -> bool:
    current = get_opds_source(conn, source_id)
    if current is None:
        return False
    new_name = current.name if name is None else name
    new_url = current.url if url is None else url
    new_name, new_url = _validate_opds_source(new_name, new_url)

    fields = ["name = ?", "url = ?", "updated_at = ?"]
    values: list[object] = [new_name, new_url, utc_now()]
    if username is not None:
        fields.append("username = ?")
        values.append(username.strip())
    if password is not None:
        fields.append("password = ?")
        values.append(password)
    if enabled is not None:
        fields.append("enabled = ?")
        values.append(1 if enabled else 0)
    values.append(source_id)
    with conn:
        conn.execute(
            f"UPDATE opds_sources SET {', '.join(fields)} WHERE id = ?", values
        )
    return True


def delete_opds_source(conn: sqlite3.Connection, source_id: int) -> bool:
    with conn:
        cur = conn.execute("DELETE FROM opds_sources WHERE id = ?", (source_id,))
    return cur.rowcount > 0


def mark_opds_source(
    conn: sqlite3.Connection,
    source_id: int,
    *,
    error: str = "",
) -> None:
    with conn:
        conn.execute(
            "UPDATE opds_sources SET last_checked_at = ?, last_error = ? WHERE id = ?",
            (utc_now(), error[:2000], source_id),
        )


def count_opds_sources(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) AS n FROM opds_sources").fetchone()["n"])


def count_authors(conn: sqlite3.Connection) -> int:    return int(
        conn.execute(
            "SELECT COUNT(DISTINCT a.id) AS n FROM authors a "
            "JOIN book_authors ba ON ba.author_id = a.id "
            "JOIN books b ON b.id = ba.book_id WHERE b.parse_error IS NULL"
        ).fetchone()["n"]
    )


def count_genres(conn: sqlite3.Connection) -> int:
    return int(
        conn.execute(
            "SELECT COUNT(DISTINCT g.id) AS n FROM genres g "
            "JOIN book_genres bg ON bg.genre_id = g.id "
            "JOIN books b ON b.id = bg.book_id WHERE b.parse_error IS NULL"
        ).fetchone()["n"]
    )


def count_series(conn: sqlite3.Connection) -> int:
    return int(
        conn.execute(
            "SELECT COUNT(DISTINCT s.id) AS n FROM series s "
            "JOIN books b ON b.series_id = s.id WHERE b.parse_error IS NULL"
        ).fetchone()["n"]
    )


def count_books(conn: sqlite3.Connection) -> int:
    return int(
        conn.execute("SELECT COUNT(*) AS n FROM books WHERE parse_error IS NULL").fetchone()["n"]
    )


def reindex_fts(conn: sqlite3.Connection) -> int:
    """Перестроить полнотекстовый индекс. Возвращает число книг."""
    if not has_fts(conn):
        return 0
    with conn:
        conn.execute("DELETE FROM books_fts")
    rows = conn.execute(
        "SELECT id, title, annotation, keywords, series_id FROM books WHERE parse_error IS NULL"
    ).fetchall()
    count = 0
    with conn:
        for row in rows:
            authors = [
                str(item["display_name"])
                for item in conn.execute(
                    "SELECT a.display_name FROM authors a "
                    "JOIN book_authors ba ON ba.author_id = a.id "
                    "WHERE ba.book_id = ? ORDER BY ba.position",
                    (row["id"],),
                )
            ]
            series = ""
            if row["series_id"] is not None:
                series_row = conn.execute(
                    "SELECT name FROM series WHERE id = ?", (row["series_id"],)
                ).fetchone()
                series = str(series_row["name"]) if series_row else ""
            conn.execute(
                "INSERT INTO books_fts(book_id, title, annotation, authors, series) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    row["id"],
                    row["title"],
                    "\n".join(
                        part
                        for part in (row["annotation"], row["keywords"])
                        if part
                    ),
                    ", ".join(authors),
                    series,
                ),
            )
            count += 1
    return count
