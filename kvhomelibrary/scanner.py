"""Сканирование каталога с книгами и наполнение БД."""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .db import (
    ScanOutcome,
    connect_db,
    delete_missing,
    init_db,
    record_parse_error,
    set_meta,
    upsert_book,
    utc_now,
)
from .fb2 import Fb2Error, iter_fb2_files, parse_fb2_bytes

ProgressCallback = Callable[[int, int, Path, str], None]


@dataclass(slots=True)
class ScanReport:
    root: str = ""
    total: int = 0
    added: int = 0
    updated: int = 0
    skipped: int = 0
    failed: int = 0
    removed: int = 0
    duration: float = 0.0
    errors: list[tuple[str, str]] = field(default_factory=list)

    @property
    def outcome(self) -> ScanOutcome:
        return ScanOutcome(
            added=self.added,
            updated=self.updated,
            skipped=self.skipped,
            failed=self.failed,
            removed=self.removed,
            total=self.total,
        )


def scan_library(
    db_path: str | Path,
    root: str | Path,
    *,
    recursive: bool = True,
    prune: bool = False,
    covers_dir: str | Path | None = None,
    progress: ProgressCallback | None = None,
) -> ScanReport:
    """Пройти по каталогу, разобрать новые/изменённые FB2 и обновить БД."""
    started = time.monotonic()
    root_path = Path(root).expanduser().resolve()
    if not root_path.is_dir():
        raise NotADirectoryError(f"каталог не найден: {root_path}")

    db_file = init_db(db_path)
    covers = Path(covers_dir) if covers_dir else db_file.parent / "covers"
    covers.mkdir(parents=True, exist_ok=True)

    files = sorted(iter_fb2_files(root_path, recursive=recursive), key=lambda p: str(p).casefold())
    report = ScanReport(root=str(root_path), total=len(files))
    seen: list[str] = []

    conn = connect_db(db_file)
    try:
        for index, path in enumerate(files, start=1):
            resolved = str(path.resolve())
            seen.append(resolved)
            status = "imported"
            try:
                stat = path.stat()
                data = path.read_bytes()
                digest = hashlib.sha256(data).hexdigest()

                existing = conn.execute(
                    "SELECT file_hash FROM books WHERE file_path = ?", (resolved,)
                ).fetchone()
                if existing is not None and existing["file_hash"] == digest:
                    report.skipped += 1
                    status = "skipped"
                else:
                    book = parse_fb2_bytes(data, path)
                    _, action = upsert_book(
                        conn,
                        book,
                        mtime=stat.st_mtime,
                        covers_dir=covers,
                    )
                    if action == "added":
                        report.added += 1
                        status = "added"
                    elif action == "duplicate":
                        report.skipped += 1
                        status = "skipped"
                    else:
                        report.updated += 1
                        status = "updated"
            except Fb2Error as exc:
                report.failed += 1
                report.errors.append((str(path), str(exc)))
                status = "error"
                try:
                    stat = path.stat()
                    record_parse_error(
                        conn,
                        path,
                        str(exc),
                        file_hash=digest,
                        file_size=stat.st_size,
                        mtime=stat.st_mtime,
                    )
                except OSError as stat_exc:
                    report.errors.append((str(path), f"не удалось прочитать файл: {stat_exc}"))
            except OSError as exc:
                report.failed += 1
                report.errors.append((str(path), f"ошибка чтения: {exc}"))
                status = "error"

            if progress is not None:
                progress(index, len(files), path, status)

        if prune:
            report.removed = delete_missing(conn, seen)

        set_meta(conn, "library_root", str(root_path))
        set_meta(conn, "last_scan", utc_now())
        set_meta(conn, "last_scan_total", str(report.total))
        set_meta(conn, "last_scan_failed", str(report.failed))
    finally:
        conn.close()

    report.duration = time.monotonic() - started
    return report
