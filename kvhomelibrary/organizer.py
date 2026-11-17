"""Утилита преобразования существующего каталога книг в новую структуру."""

from __future__ import annotations

import hashlib
import logging
import shutil
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .book_formats import build_book
from .db import connect_db, init_db, upsert_book
from .fb2 import Fb2Error, ParsedBook
from .storage import ensure_parent, target_path

LOGGER = logging.getLogger("kvhomelibrary.organizer")

_DIRECT_FORMATS = {"fb2": "fb2", "pdf": "pdf", "djvu": "djvu", "epub": "epub"}


@dataclass(slots=True)
class OrganizeReport:
    root: str = ""
    total: int = 0
    moved: int = 0
    copied: int = 0
    already: int = 0
    duplicates: int = 0
    added: int = 0
    updated: int = 0
    failed: int = 0
    errors: list[tuple[str, str]] = field(default_factory=list)


def _supported(path: Path) -> bool:
    lowered = path.name.casefold()
    if lowered.endswith(".fb2.zip"):
        return True
    suffix = path.suffix.casefold().lstrip(".")
    return suffix in _DIRECT_FORMATS or suffix == "zip"


def _load(path: Path) -> tuple[ParsedBook, bool]:
    """Вернуть книгу и признак того, что файл является ZIP-контейнером."""
    lowered = path.name.casefold()
    data = path.read_bytes()
    if lowered.endswith(".fb2.zip"):
        book = build_book(data, path.name, "fb2", str(path))
        return book, True
    suffix = path.suffix.casefold().lstrip(".")
    if suffix in _DIRECT_FORMATS:
        book = build_book(data, path.name, suffix, str(path))
        return book, False
    if suffix == "zip":
        with zipfile.ZipFile(path) as archive:
            names = [
                name for name in archive.namelist()
                if not name.endswith("/")
                and Path(name).suffix.casefold().lstrip(".") in _DIRECT_FORMATS
            ]
            if len(names) != 1:
                raise ValueError("ZIP должен содержать ровно один файл книги")
            inner = names[0]
            fmt = Path(inner).suffix.casefold().lstrip(".")
            book = build_book(archive.read(inner), Path(inner).name, fmt, str(path))
            return book, True
    raise ValueError(f"неподдерживаемый формат: {path.suffix}")


def _same_content(path: Path, digest: str) -> bool:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest() == digest
    except OSError:
        return False


def _remove_empty_dirs(root: Path) -> int:
    removed = 0
    for directory in sorted(
        (item for item in root.rglob("*") if item.is_dir()),
        key=lambda item: len(item.parts),
        reverse=True,
    ):
        if directory == root or directory.name in {".downloads", ".git", ".venv"}:
            continue
        try:
            if not any(directory.iterdir()):
                directory.rmdir()
                removed += 1
        except OSError:
            continue
    return removed


def organize_library(
    db_path: str | Path,
    library_dir: str | Path,
    *,
    covers_dir: str | Path | None = None,
    copy: bool = False,
    dry_run: bool = False,
    prune_empty: bool = False,
    progress: Callable[[int, int, Path], None] | None = None,
) -> OrganizeReport:
    """Переместить/скопировать книги в ``язык/категория/серия``."""
    library = Path(library_dir).expanduser().resolve()
    if not library.is_dir():
        raise NotADirectoryError(f"каталог не найден: {library}")

    report = OrganizeReport(root=str(library))
    files = sorted(
        (path for path in library.rglob("*") if path.is_file() and _supported(path)),
        key=lambda item: str(item).casefold(),
    )
    report.total = len(files)

    conn = None
    covers = Path(covers_dir).expanduser().resolve() if covers_dir else None
    if not dry_run:
        init_db(db_path)
        conn = connect_db(db_path)

    try:
        for index, source in enumerate(files, start=1):
            if progress is not None:
                progress(index, len(files), source)
            try:
                book, archive_zip = _load(source)
                destination = target_path(
                    library,
                    book,
                    archive_zip=archive_zip,
                    original_name=source.name,
                )
                if destination == source:
                    report.already += 1
                    if conn is not None:
                        _, status = upsert_book(
                            conn,
                            book,
                            mtime=source.stat().st_mtime,
                            covers_dir=covers,
                        )
                        if status == "added":
                            report.added += 1
                        elif status == "updated":
                            report.updated += 1
                    continue

                if destination.exists():
                    if _same_content(destination, book.file_hash):
                        report.duplicates += 1
                        if conn is not None:
                            book.source_path = str(destination)
                            _, status = upsert_book(
                                conn,
                                book,
                                mtime=destination.stat().st_mtime,
                                covers_dir=covers,
                            )
                            if status == "added":
                                report.added += 1
                            elif status == "updated":
                                report.updated += 1
                        if not copy:
                            source.unlink(missing_ok=True)
                        continue
                    destination = destination.with_name(
                        f"{destination.stem}-{book.file_hash[:8]}{destination.suffix}"
                    )

                if dry_run:
                    if copy:
                        report.copied += 1
                    else:
                        report.moved += 1
                    continue

                ensure_parent(destination)
                if copy:
                    shutil.copy2(source, destination)
                    report.copied += 1
                else:
                    shutil.move(str(source), str(destination))
                    report.moved += 1

                book.source_path = str(destination)
                if conn is not None:
                    _, status = upsert_book(
                        conn,
                        book,
                        mtime=destination.stat().st_mtime,
                        covers_dir=covers,
                    )
                    if status == "added":
                        report.added += 1
                    elif status == "updated":
                        report.updated += 1
            except (OSError, ValueError, Fb2Error, zipfile.BadZipFile) as exc:
                report.failed += 1
                report.errors.append((str(source), str(exc)))
                LOGGER.warning("Не удалось разложить %s: %s", source, exc)
            except Exception as exc:  # noqa: BLE001 - одна книга не должна останавливать миграцию
                report.failed += 1
                report.errors.append((str(source), str(exc)))
                LOGGER.exception("Ошибка миграции %s", source)
    finally:
        if conn is not None:
            conn.close()

    if prune_empty and not dry_run:
        _remove_empty_dirs(library)
    return report
