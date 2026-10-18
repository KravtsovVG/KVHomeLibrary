"""Импорт книг из ZIP/7Z/RAR-архивов в библиотеку.

Для ZIP используется стандартная библиотека Python. Для 7Z/RAR нужен
внешний `7z` (7-Zip/p7zip) или `unrar`; путь к инструменту ищется в PATH
и стандартных каталогах Windows.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import zipfile
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Callable, Iterable, Iterator

from .book_formats import FORMATS, build_book
from .db import connect_db, init_db, upsert_book
from .fb2 import Fb2Error

ARCHIVE_SUFFIXES = {".zip", ".7z", ".rar"}
MAX_ENTRY_BYTES = 512 * 1024 * 1024
MAX_ENTRIES = 5000
TOOL_TIMEOUT = 300


class ArchiveError(Exception):
    """Ошибка чтения архива или отсутствия нужного инструмента."""


@dataclass(slots=True)
class ArchiveScanReport:
    directory: str
    archives_seen: int = 0
    archives_total: int = 0
    archives_done: int = 0
    current_archive: str = ""
    progress: int = 0
    archives_skipped_date: int = 0
    files_seen: int = 0
    imported: int = 0
    updated: int = 0
    duplicates: int = 0
    failed: int = 0
    skipped_types: int = 0
    errors: list[str] = field(default_factory=list)

    def summary(self) -> str:
        parts = [
            f"архивов: {self.archives_seen}",
            f"файлов: {self.files_seen}",
            f"добавлено: {self.imported}",
            f"обновлено: {self.updated}",
            f"дубликатов: {self.duplicates}",
            f"ошибок: {self.failed}",
        ]
        if self.archives_skipped_date:
            parts.append(f"пропущено по дате: {self.archives_skipped_date}")
        if self.skipped_types:
            parts.append(f"пропущено по типу: {self.skipped_types}")
        text = "; ".join(parts)
        if self.errors:
            text += ". " + " | ".join(self.errors[:3])
        return text


def archive_created_date(path: str | Path) -> date:
    stat = Path(path).stat()
    timestamp = getattr(stat, "st_birthtime", None)
    if timestamp is None:
        timestamp = stat.st_ctime
    return datetime.fromtimestamp(timestamp).date()


def _iter_archives(
    root: Path,
    recursive: bool,
    excluded: Iterable[Path],
) -> Iterator[Path]:
    excluded_list = [item.resolve() for item in excluded]

    def is_excluded(path: Path) -> bool:
        resolved = path.resolve()
        return any(
            resolved == item or item in resolved.parents
            for item in excluded_list
        )

    if recursive:
        for current, dirs, files in os.walk(root):
            current_path = Path(current)
            dirs[:] = [
                name for name in dirs
                if not is_excluded(current_path / name)
            ]
            for name in files:
                path = current_path / name
                if path.suffix.casefold() in ARCHIVE_SUFFIXES and not is_excluded(path):
                    yield path
    else:
        for path in root.iterdir():
            if path.is_file() and path.suffix.casefold() in ARCHIVE_SUFFIXES:
                if not is_excluded(path):
                    yield path


def _find_7z() -> str | None:
    for name in ("7z", "7zz", "7za"):
        found = shutil.which(name)
        if found:
            return found
    candidates = [
        Path(os.environ.get("ProgramFiles", "")) / "7-Zip" / "7z.exe",
        Path(os.environ.get("ProgramFiles(x86)", "")) / "7-Zip" / "7z.exe",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return None


def _run_tool(args: list[str]) -> subprocess.CompletedProcess[bytes]:
    try:
        return subprocess.run(
            args,
            capture_output=True,
            timeout=TOOL_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ArchiveError(f"не удалось запустить {args[0]}: {exc}") from exc


def _external_entries(path: Path) -> Iterator[tuple[str, bytes]]:
    tool = _find_7z()
    if tool:
        listing = _run_tool([tool, "l", "-slt", "-ba", "-sccUTF-8", str(path)])
        if listing.returncode != 0:
            message = listing.stderr.decode("utf-8", errors="replace").strip()
            raise ArchiveError(message or "7z не смог прочитать архив")
        entries: list[tuple[str, int]] = []
        current_name = ""
        current_size = 0
        current_is_dir = False

        def flush_entry() -> None:
            nonlocal current_name, current_size, current_is_dir
            if current_name and not current_is_dir:
                entries.append((current_name, current_size))
            current_name = ""
            current_size = 0
            current_is_dir = False

        for raw_line in listing.stdout.decode("utf-8", errors="replace").splitlines():
            line = raw_line.strip()
            if not line:
                flush_entry()
                continue
            if " = " not in line:
                continue
            key, value = line.split(" = ", 1)
            if key == "Path":
                flush_entry()
                current_name = value
                current_is_dir = value.endswith("/") or value.endswith("\\")
            elif key == "Size":
                try:
                    current_size = int(value)
                except ValueError:
                    current_size = 0
            elif key == "Attributes":
                current_is_dir = "D" in value or "Directory" in value
            elif key == "Folder":
                current_is_dir = current_is_dir or value.strip() == "+"
        flush_entry()

        for name, size in entries[:MAX_ENTRIES]:
            if size > MAX_ENTRY_BYTES:
                raise ArchiveError(f"слишком большой файл в архиве: {name}")
            result = _run_tool([tool, "x", "-so", "-y", "-sccUTF-8", str(path), name])
            if result.returncode != 0:
                message = result.stderr.decode("utf-8", errors="replace").strip()
                raise ArchiveError(message or f"не удалось извлечь {name}")
            yield name, result.stdout
        return

    unrar = shutil.which("unrar")
    if unrar and path.suffix.casefold() == ".rar":
        listing = _run_tool([unrar, "lb", "-c-", str(path)])
        if listing.returncode != 0:
            message = listing.stderr.decode("utf-8", errors="replace").strip()
            raise ArchiveError(message or "unrar не смог прочитать архив")
        names = [
            line.strip()
            for line in listing.stdout.decode("utf-8", errors="replace").splitlines()
            if line.strip()
        ]
        for name in names[:MAX_ENTRIES]:
            result = _run_tool([unrar, "p", "-inul", "-p-", str(path), name])
            if result.returncode != 0:
                message = result.stderr.decode("utf-8", errors="replace").strip()
                raise ArchiveError(message or f"не удалось извлечь {name}")
            if len(result.stdout) > MAX_ENTRY_BYTES:
                raise ArchiveError(f"слишком большой файл в архиве: {name}")
            yield name, result.stdout
        return

    raise ArchiveError("для 7Z/RAR нужен 7-Zip (7z) или unrar в PATH")


def iter_archive_entries(path: str | Path) -> Iterator[tuple[str, bytes]]:
    archive_path = Path(path)
    if archive_path.suffix.casefold() == ".zip":
        try:
            with zipfile.ZipFile(archive_path) as archive:
                infos = [info for info in archive.infolist() if not info.is_dir()]
                for info in infos[:MAX_ENTRIES]:
                    if info.flag_bits & 0x1:
                        raise ArchiveError("ZIP-архив защищён паролем")
                    if info.file_size > MAX_ENTRY_BYTES:
                        raise ArchiveError(f"слишком большой файл в архиве: {info.filename}")
                    yield info.filename, archive.read(info)
        except (zipfile.BadZipFile, RuntimeError, OSError) as exc:
            raise ArchiveError(f"не удалось прочитать ZIP: {exc}") from exc
        return
    yield from _external_entries(archive_path)


def _write_zip(target: Path, name: str, data: bytes) -> None:
    if target.exists():
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    try:
        with zipfile.ZipFile(
            temporary,
            "w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=9,
        ) as archive:
            archive.writestr(Path(name).name or "book", data)
        os.replace(temporary, target)
    finally:
        if temporary.exists():
            temporary.unlink(missing_ok=True)


def _entry_format(name: str) -> str:
    normalized = name.replace("\\", "/")
    return Path(normalized).suffix.casefold().lstrip(".")


def _entry_basename(name: str) -> str:
    return name.replace("\\", "/").rsplit("/", 1)[-1]


def scan_archives(
    db_path: str | Path,
    directory: str | Path,
    *,
    file_types: Iterable[str] = FORMATS,
    date_from: date | None = None,
    date_to: date | None = None,
    recursive: bool = True,
    library_dir: str | Path,
    covers_dir: str | Path | None = None,
    on_progress: Callable[[ArchiveScanReport], None] | None = None,
) -> ArchiveScanReport:
    root = Path(directory).expanduser().resolve()
    if not root.is_dir():
        raise ArchiveError(f"каталог не найден: {root}")
    if date_from and date_to and date_from > date_to:
        raise ArchiveError("начальная дата больше конечной")

    requested = {(item or "").casefold().lstrip(".") for item in file_types}
    requested &= set(FORMATS)
    if not requested:
        raise ArchiveError("не выбран ни один тип файлов")

    library = Path(library_dir).expanduser().resolve()
    imported = library / "imported"
    report = ArchiveScanReport(directory=str(root))
    init_db(db_path)
    conn = connect_db(db_path)

    archives = list(_iter_archives(root, recursive, [imported]))
    report.archives_total = len(archives)

    def notify() -> None:
        if on_progress is None:
            return
        try:
            on_progress(report)
        except Exception:
            # Прогресс не должен ломать сам импорт.
            pass

    def finish_archive() -> None:
        report.archives_done += 1
        report.progress = (
            100 if not report.archives_total
            else round(100 * report.archives_done / report.archives_total)
        )
        notify()

    notify()
    try:
        for archive_path in archives:
            report.archives_seen += 1
            report.current_archive = archive_path.name
            notify()
            try:
                created = archive_created_date(archive_path)
            except OSError as exc:
                report.failed += 1
                report.errors.append(f"{archive_path.name}: {exc}")
                finish_archive()
                continue
            if date_from and created < date_from:
                report.archives_skipped_date += 1
                finish_archive()
                continue
            if date_to and created > date_to:
                report.archives_skipped_date += 1
                finish_archive()
                continue

            try:
                for name, data in iter_archive_entries(archive_path):
                    fmt = _entry_format(name)
                    if fmt not in requested:
                        report.skipped_types += 1
                        notify()
                        continue
                    if len(data) > MAX_ENTRY_BYTES:
                        report.failed += 1
                        report.errors.append(f"{archive_path.name}/{name}: файл слишком большой")
                        notify()
                        continue
                    report.files_seen += 1
                    try:
                        digest = hashlib.sha256(data).hexdigest()
                        target = imported / f"{digest}.zip"
                        original_name = _entry_basename(name) or f"book.{fmt}"
                        _write_zip(target, original_name, data)
                        book = build_book(data, original_name, fmt, str(target))
                        _, status = upsert_book(
                            conn,
                            book,
                            mtime=target.stat().st_mtime,
                            covers_dir=covers_dir,
                        )
                        if status == "added":
                            report.imported += 1
                        elif status == "updated":
                            report.updated += 1
                        else:
                            report.duplicates += 1
                    except (Fb2Error, ArchiveError, OSError, ValueError) as exc:
                        report.failed += 1
                        report.errors.append(f"{archive_path.name}/{name}: {exc}")
                    notify()
            except ArchiveError as exc:
                report.failed += 1
                report.errors.append(f"{archive_path.name}: {exc}")
            finish_archive()
    finally:
        conn.close()
    report.current_archive = ""
    report.progress = 100
    notify()
    return report
