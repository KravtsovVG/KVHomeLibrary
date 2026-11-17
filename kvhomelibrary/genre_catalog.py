"""Приведение жанров в базе к кодам FictionBook 2.1."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Iterable

from .db import GenreRef, list_all_genres, merge_genres
from .genres import (
    FB2_GENRE_NAMES,
    GENRE_ALIASES,
    GENRE_SYNONYMS,
    genre_name,
    normalize_genre,
)

LOGGER = logging.getLogger("kvhomelibrary.genre_catalog")


@dataclass(slots=True)
class GenreChange:
    old_code: str
    old_name: str
    new_code: str
    new_name: str
    action: str
    books: int = 0


@dataclass(slots=True)
class GenreNormalizationReport:
    total: int = 0
    renamed: int = 0
    merged: int = 0
    unchanged: int = 0
    unknown: int = 0
    changes: list[GenreChange] = field(default_factory=list)
    unknown_genres: list[tuple[str, str]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def _key(value: str) -> str:
    return " ".join((value or "").replace("ё", "е").casefold().split())


def _build_lookup() -> tuple[dict[str, str], dict[str, str], dict[str, str]]:
    codes: dict[str, str] = {}
    names: dict[str, str] = {}
    synonyms: dict[str, str] = {}
    for code, name in FB2_GENRE_NAMES.items():
        codes[_key(code)] = code
        names[_key(name)] = code
    for alias, target in GENRE_ALIASES.items():
        canonical = normalize_genre(target)
        if canonical in FB2_GENRE_NAMES:
            codes[_key(alias)] = canonical
    for code, values in GENRE_SYNONYMS.items():
        canonical = normalize_genre(code)
        if canonical not in FB2_GENRE_NAMES:
            continue
        for value in values:
            synonyms[_key(value)] = canonical
    return codes, names, synonyms


_LOOKUP = _build_lookup()


def find_fb2_genre(*values: str) -> str | None:
    """Найти канонический код FB2 по коду, названию или синониму."""
    codes, names, synonyms = _LOOKUP
    for value in values:
        raw = (value or "").strip()
        if not raw:
            continue
        key = _key(raw)
        if key in codes:
            return codes[key]
        normalized = normalize_genre(raw)
        if normalized in FB2_GENRE_NAMES:
            return normalized
        if key in names:
            return names[key]
        if key in synonyms:
            return synonyms[key]
        underscored = _key(raw.replace("-", "_").replace(" ", "_"))
        if underscored in codes:
            return codes[underscored]
    return None


def _canonical_name(code: str) -> str:
    return FB2_GENRE_NAMES.get(code) or genre_name(code)


def normalize_fb2_genres(
    conn,
    *,
    dry_run: bool = False,
) -> GenreNormalizationReport:
    """Объединить и переименовать жанры по спецификации FB2 2.1."""
    rows = list_all_genres(conn)
    report = GenreNormalizationReport(total=len(rows))
    groups: dict[str, list[GenreRef]] = {}
    for genre in rows:
        code = find_fb2_genre(genre.code, genre.name, genre.display_name)
        if code is None:
            report.unknown += 1
            report.unknown_genres.append((genre.code, genre.name))
            continue
        groups.setdefault(code, []).append(genre)

    for code, group in groups.items():
        target = next((item for item in group if item.code == code), None)
        if target is None:
            target = group[0]
            report.renamed += 1
            report.changes.append(
                GenreChange(
                    old_code=target.code,
                    old_name=target.name,
                    new_code=code,
                    new_name=_canonical_name(code),
                    action="rename",
                    books=target.books,
                )
            )
        else:
            report.unchanged += 1

        if not dry_run:
            with conn:
                conn.execute(
                    "UPDATE genres SET code = ?, name = ?, sort_key = ?, "
                    "category_path = CASE WHEN category_path = '' THEN ? ELSE category_path END "
                    "WHERE id = ?",
                    (
                        code,
                        _canonical_name(code),
                        _key(target.display_name or _canonical_name(code)),
                        _canonical_name(code),
                        target.id,
                    ),
                )
            LOGGER.info("Жанр %s → %s (%s)", target.code, code, _canonical_name(code))

        for source in group:
            if source.id == target.id:
                continue
            report.merged += 1
            report.changes.append(
                GenreChange(
                    old_code=source.code,
                    old_name=source.name,
                    new_code=code,
                    new_name=_canonical_name(code),
                    action="merge",
                    books=source.books,
                )
            )
            if dry_run:
                continue
            try:
                merge_genres(conn, source.id, target.id)
            except ValueError as exc:
                report.errors.append(f"{source.code}: {exc}")
                LOGGER.warning("Не удалось объединить жанр %s: %s", source.code, exc)
                continue
            LOGGER.info("Жанр %s объединён с %s", source.code, code)

    return report


def list_fb2_genres() -> Iterable[tuple[str, str, tuple[str, ...]]]:
    """Канонические коды, русские названия и синонимы."""
    for code in sorted(FB2_GENRE_NAMES):
        yield code, FB2_GENRE_NAMES[code], tuple(GENRE_SYNONYMS.get(code, ()))
