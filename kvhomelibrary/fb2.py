"""Разбор файлов FictionBook 2 (.fb2 и .fb2.zip).

Модуль не зависит от сторонних библиотек: используется стандартный
``xml.etree.ElementTree``. Поддерживаются пространства имён FB2, разные
кодировки (UTF-8, Windows-1251, KOI8-R и др.), обложки в ``<binary>``
и книги внутри ZIP-архивов.
"""

from __future__ import annotations

import base64
import hashlib
import html
import io
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

import xml.etree.ElementTree as ET

FB2_NS = "http://www.gribuser.ru/xml/fictionbook/2.0"
XLINK_NS = "http://www.w3.org/1999/xlink"

_XML_DECL_RE = re.compile(rb"<\?xml[^>]*encoding=[\"']([A-Za-z0-9._\-]+)[\"']", re.I)
_XML_DECL_TEXT_RE = re.compile(r"^\ufeff?\s*<\?xml[^>]*\?>", re.I | re.S)
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_SPACE_RE = re.compile(r"\s+")

_IMAGE_EXTENSIONS = {
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/png": "png",
    "image/gif": "gif",
    "image/webp": "webp",
    "image/svg+xml": "svg",
    "image/bmp": "bmp",
}


class Fb2Error(ValueError):
    """Файл не является корректной FB2-книгой."""


@dataclass(slots=True)
class Fb2Author:
    first_name: str = ""
    middle_name: str = ""
    last_name: str = ""
    nickname: str = ""
    email: str = ""
    home_page: str = ""

    @property
    def display_name(self) -> str:
        parts = [self.first_name, self.middle_name, self.last_name]
        name = " ".join(part for part in parts if part).strip()
        return name or self.nickname or "Неизвестный автор"

    @property
    def sort_name(self) -> str:
        parts = [self.last_name, self.first_name, self.middle_name]
        name = " ".join(part for part in parts if part).strip()
        return name or self.nickname or "Неизвестный автор"

    def is_empty(self) -> bool:
        return not any(
            (self.first_name, self.middle_name, self.last_name, self.nickname)
        )


@dataclass(slots=True)
class ParsedBook:
    """Полные метаданные книги, извлечённые из FB2."""

    title: str
    sort_key: str
    authors: list[Fb2Author] = field(default_factory=list)
    genres: list[str] = field(default_factory=list)
    annotation: str = ""
    keywords: str = ""
    lang: str = ""
    src_lang: str = ""
    book_date: str = ""
    series: str = ""
    series_number: str = ""
    publisher: str = ""
    city: str = ""
    publish_year: str = ""
    isbn: str = ""
    doc_id: str = ""
    doc_version: str = ""
    doc_date: str = ""
    translators: str = ""
    cover_data: bytes | None = None
    cover_mime: str = ""
    cover_ext: str = ""
    content_html: str = ""
    toc: list[dict[str, str]] = field(default_factory=list)
    file_hash: str = ""
    file_size: int = 0
    source_path: str = ""
    file_name: str = ""


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _clean(value: str | None) -> str:
    if not value:
        return ""
    value = _CONTROL_RE.sub("", value)
    return _SPACE_RE.sub(" ", value).strip()


def _text(elem: ET.Element | None) -> str:
    if elem is None:
        return ""
    return _clean(" ".join(elem.itertext()))


def _child(elem: ET.Element | None, name: str) -> ET.Element | None:
    if elem is None:
        return None
    for child in elem:
        if _local(child.tag) == name:
            return child
    return None


def _children(elem: ET.Element | None, name: str) -> list[ET.Element]:
    if elem is None:
        return []
    return [child for child in elem if _local(child.tag) == name]


def _path(elem: ET.Element | None, *names: str) -> ET.Element | None:
    current = elem
    for name in names:
        current = _child(current, name)
        if current is None:
            return None
    return current


def _attr(elem: ET.Element | None, name: str) -> str:
    if elem is None:
        return ""
    return _clean(elem.get(name))


def _decode_xml(data: bytes) -> str:
    declared: str | None = None
    match = _XML_DECL_RE.search(data[:512])
    if match:
        try:
            declared = match.group(1).decode("ascii")
        except UnicodeDecodeError:
            declared = None

    encodings: list[str] = []
    if declared:
        encodings.append(declared)
    encodings.extend(["utf-8-sig", "cp1251", "koi8-r", "latin-1"])

    seen: set[str] = set()
    for encoding in encodings:
        key = encoding.lower()
        if key in seen:
            continue
        seen.add(key)
        try:
            return data.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
    return data.decode("utf-8", errors="replace")


def _parse_xml(data: bytes) -> ET.Element:
    try:
        return ET.fromstring(data)
    except ET.ParseError:
        pass

    # Запасной путь: декодируем текст, убираем XML-декларацию (в ней может
    # быть указана кодировка, отличная от фактической) и разбираем снова.
    text = _decode_xml(data)
    text = _XML_DECL_TEXT_RE.sub("", text, count=1)
    try:
        return ET.fromstring(text)
    except ET.ParseError as exc:
        raise Fb2Error(f"не удалось разобрать XML: {exc}") from exc


def _extract_fb2(data: bytes) -> bytes:
    if not zipfile.is_zipfile(io.BytesIO(data)):
        return data
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            names = [
                name
                for name in archive.namelist()
                if name.lower().endswith(".fb2") and not name.endswith("/")
            ]
            if not names:
                raise Fb2Error("в ZIP-архиве нет файла .fb2")
            names.sort(key=lambda item: (item.count("/"), item.lower()))
            return archive.read(names[0])
    except zipfile.BadZipFile as exc:
        raise Fb2Error(f"повреждён ZIP-архив: {exc}") from exc


def _author_from(elem: ET.Element) -> Fb2Author:
    return Fb2Author(
        first_name=_text(_child(elem, "first-name")),
        middle_name=_text(_child(elem, "middle-name")),
        last_name=_text(_child(elem, "last-name")),
        nickname=_text(_child(elem, "nickname")),
        email=_text(_child(elem, "email")),
        home_page=_text(_child(elem, "home-page")),
    )


def _sequence(elem: ET.Element | None) -> tuple[str, str]:
    seq = _child(elem, "sequence")
    if seq is None:
        return "", ""
    name = _clean(seq.get("name"))
    number = _clean(seq.get("number"))
    return name, number


def _sniff_image_mime(data: bytes) -> str:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"GIF87a") or data.startswith(b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data.startswith(b"BM"):
        return "image/bmp"
    return "image/jpeg"


def _extract_cover(root: ET.Element, title_info: ET.Element | None) -> tuple[bytes | None, str, str]:
    coverpage = _child(title_info, "coverpage")
    if coverpage is None:
        return None, "", ""
    image = _child(coverpage, "image")
    if image is None:
        return None, "", ""

    href = image.get(f"{{{XLINK_NS}}}href") or image.get("href") or ""
    href = href.strip()
    if not href.startswith("#"):
        return None, "", ""
    binary_id = href[1:]

    binary = None
    for child in root:
        if _local(child.tag) == "binary" and child.get("id") == binary_id:
            binary = child
            break
    if binary is None or not binary.text:
        return None, "", ""

    try:
        raw = re.sub(rb"\s+", b"", binary.text.encode("ascii", errors="ignore"))
        data = base64.b64decode(raw, validate=False)
    except (ValueError, TypeError):
        return None, "", ""
    if not data:
        return None, "", ""

    mime = _clean(binary.get("content-type")).lower().split(";", 1)[0].strip()
    if not mime.startswith("image/"):
        mime = _sniff_image_mime(data)
    ext = _IMAGE_EXTENSIONS.get(mime, "bin")
    return data, mime, ext


def _title_sort_key(title: str) -> str:
    value = (title or "").casefold().strip()
    value = re.sub(r"^(?:the|a|an)\s+", "", value)
    value = re.sub(r"[«»\"'`]", "", value)
    return _SPACE_RE.sub(" ", value).strip()


def _esc(value: str) -> str:
    return html.escape(value or "", quote=True)


def _inline_html(elem: ET.Element | None) -> str:
    if elem is None:
        return ""
    parts = [html.escape(elem.text or "", quote=False)]
    for child in elem:
        tag = _local(child.tag)
        inner = _inline_html(child)
        if tag in {"strong", "bold"}:
            parts.append(f"<strong>{inner}</strong>")
        elif tag in {"emphasis", "em", "i"}:
            parts.append(f"<em>{inner}</em>")
        elif tag == "strikethrough":
            parts.append(f"<s>{inner}</s>")
        elif tag == "sub":
            parts.append(f"<sub>{inner}</sub>")
        elif tag == "sup":
            parts.append(f"<sup>{inner}</sup>")
        elif tag == "code":
            parts.append(f"<code>{inner}</code>")
        elif tag == "a":
            href = (
                child.get(f"{{{XLINK_NS}}}href")
                or child.get("href")
                or ""
            ).strip()
            if href.startswith("#"):
                cls = ' class="reader-note"' if child.get("type") == "note" else ""
                parts.append(f'<a href="{_esc(href)}"{cls}>{inner}</a>')
            elif href.startswith(("http://", "https://")):
                parts.append(
                    f'<a href="{_esc(href)}" target="_blank" rel="noopener noreferrer">{inner}</a>'
                )
            else:
                parts.append(inner)
        elif tag == "image":
            alt = _clean(child.get("alt")) or _clean(child.get("title")) or "Изображение"
            parts.append(f'<span class="reader-image-placeholder">{_esc(alt)}</span>')
        else:
            parts.append(inner)
        parts.append(html.escape(child.tail or "", quote=False))
    return "".join(parts)


def _block_html(
    elem: ET.Element,
    *,
    level: int,
    toc: list[dict[str, str]],
    seen_ids: set[str],
    counter: list[int],
) -> str:
    tag = _local(elem.tag)
    if tag == "section":
        counter[0] += 1
        section_id = _clean(elem.get("id")) or f"section-{counter[0]}"
        if section_id in seen_ids:
            section_id = f"{section_id}-{counter[0]}"
        seen_ids.add(section_id)

        title_elem = _child(elem, "title")
        title = _text(title_elem)
        if title:
            toc.append({"id": section_id, "title": title, "level": str(level + 1)})

        parts = [f'<section id="{_esc(section_id)}">']
        if title_elem is not None:
            heading_level = min(level + 2, 6)
            parts.append(f"<h{heading_level}>{_inline_html(title_elem)}</h{heading_level}>")
        for child in elem:
            if child is title_elem:
                continue
            parts.append(
                _block_html(
                    child,
                    level=level + 1,
                    toc=toc,
                    seen_ids=seen_ids,
                    counter=counter,
                )
            )
        parts.append("</section>")
        return "".join(parts)

    if tag == "title":
        return f"<h2>{_inline_html(elem)}</h2>"
    if tag == "subtitle":
        return f"<h3>{_inline_html(elem)}</h3>"
    if tag == "p":
        return f"<p>{_inline_html(elem)}</p>"
    if tag == "empty-line":
        return '<div class="empty-line"></div>'
    if tag in {"epigraph", "cite"}:
        children = "".join(
            _block_html(
                child,
                level=level,
                toc=toc,
                seen_ids=seen_ids,
                counter=counter,
            )
            for child in elem
        )
        return f'<blockquote class="{tag}">{children}</blockquote>'
    if tag == "poem":
        children = "".join(
            _block_html(
                child,
                level=level,
                toc=toc,
                seen_ids=seen_ids,
                counter=counter,
            )
            for child in elem
        )
        return f'<div class="poem">{children}</div>'
    if tag == "stanza":
        children = "".join(
            _block_html(
                child,
                level=level,
                toc=toc,
                seen_ids=seen_ids,
                counter=counter,
            )
            for child in elem
        )
        return f'<div class="stanza">{children}</div>'
    if tag == "v":
        return f'<p class="verse">{_inline_html(elem)}</p>'
    if tag == "text-author":
        return f'<p class="text-author">{_inline_html(elem)}</p>'
    if tag == "annotation":
        return f'<div class="annotation">{_inline_html(elem)}</div>'
    if tag == "table":
        rows = []
        for row in elem:
            if _local(row.tag) != "tr":
                continue
            cells = []
            for cell in row:
                cell_tag = _local(cell.tag)
                if cell_tag not in {"td", "th"}:
                    continue
                cells.append(f"<{cell_tag}>{_inline_html(cell)}</{cell_tag}>")
            rows.append(f"<tr>{''.join(cells)}</tr>")
        return f"<table>{''.join(rows)}</table>"
    if tag == "image":
        alt = _clean(elem.get("alt")) or _clean(elem.get("title")) or "Изображение"
        return f'<p class="reader-image-placeholder">{_esc(alt)}</p>'

    if tag == "a":
        return f"<p>{_inline_html(elem)}</p>"

    parts: list[str] = []
    if _clean(elem.text):
        parts.append(f"<p>{html.escape(_clean(elem.text))}</p>")
    for child in elem:
        parts.append(
            _block_html(
                child,
                level=level,
                toc=toc,
                seen_ids=seen_ids,
                counter=counter,
            )
        )
        if _clean(child.tail):
            parts.append(f"<p>{html.escape(_clean(child.tail))}</p>")
    return "".join(parts)


def render_fb2_body(root: ET.Element) -> tuple[str, list[dict[str, str]]]:
    """Собрать читаемый HTML и оглавление из ``body`` FB2."""
    bodies = [child for child in root if _local(child.tag) == "body"]
    if not bodies:
        return "", []

    main_body = next(
        (body for body in bodies if _clean(body.get("name")) not in {"notes", "comments"}),
        bodies[0],
    )
    toc: list[dict[str, str]] = []
    seen_ids: set[str] = set()
    counter = [0]
    parts: list[str] = []
    for child in main_body:
        parts.append(
            _block_html(
                child,
                level=0,
                toc=toc,
                seen_ids=seen_ids,
                counter=counter,
            )
        )

    for body in bodies:
        if body is main_body:
            continue
        body_name = _clean(body.get("name"))
        title = "Примечания" if body_name in {"notes", "comments"} else "Дополнительные материалы"
        parts.append(f'<section class="notes"><h2>{title}</h2>')
        for child in body:
            parts.append(
                _block_html(
                    child,
                    level=1,
                    toc=toc,
                    seen_ids=seen_ids,
                    counter=counter,
                )
            )
        parts.append("</section>")

    return "".join(parts), toc


def parse_fb2_bytes(data: bytes, source_path: str | Path = "") -> ParsedBook:
    """Разобрать содержимое FB2 (в том числе ZIP) в :class:`ParsedBook`."""
    source = Path(source_path) if source_path else Path("book.fb2")
    inner = _extract_fb2(data)
    root = _parse_xml(inner)

    if _local(root.tag) != "FictionBook":
        raise Fb2Error("корневой элемент не FictionBook")

    description = _child(root, "description")
    title_info = _child(description, "title-info")
    document_info = _child(description, "document-info")
    publish_info = _child(description, "publish-info")

    title = _text(_child(title_info, "book-title")) or source.stem
    authors: list[Fb2Author] = []
    for author_elem in _children(title_info, "author"):
        author = _author_from(author_elem)
        if not author.is_empty():
            authors.append(author)

    genres: list[str] = []
    seen_genres: set[str] = set()
    for genre_elem in _children(title_info, "genre"):
        code = _text(genre_elem) or _clean(genre_elem.get("match"))
        if not code:
            continue
        key = code.casefold()
        if key not in seen_genres:
            seen_genres.add(key)
            genres.append(code)

    series, series_number = _sequence(title_info)
    if not series:
        series, series_number = _sequence(publish_info)

    translators: list[str] = []
    for translator_elem in _children(title_info, "translator"):
        translator = _author_from(translator_elem)
        if not translator.is_empty():
            translators.append(translator.display_name)

    date_elem = _child(title_info, "date")
    book_date = _clean(date_elem.get("value")) if date_elem is not None else ""
    if not book_date:
        book_date = _text(date_elem)

    cover_data, cover_mime, cover_ext = _extract_cover(root, title_info)
    content_html, toc = render_fb2_body(root)

    return ParsedBook(
        title=title,
        sort_key=_title_sort_key(title),
        authors=authors,
        genres=genres,
        annotation=_text(_child(title_info, "annotation")),
        keywords=_text(_child(title_info, "keywords")),
        lang=_text(_child(title_info, "lang")),
        src_lang=_text(_child(title_info, "src-lang")),
        book_date=book_date,
        series=series,
        series_number=series_number,
        publisher=_text(_child(publish_info, "publisher")),
        city=_text(_child(publish_info, "city")),
        publish_year=_text(_child(publish_info, "year")),
        isbn=_text(_child(publish_info, "isbn")) or _text(_child(document_info, "isbn")),
        doc_id=_text(_child(document_info, "id")),
        doc_version=_text(_child(document_info, "version")),
        doc_date=_text(_child(document_info, "date")),
        translators=", ".join(translators),
        cover_data=cover_data,
        cover_mime=cover_mime,
        cover_ext=cover_ext,
        content_html=content_html,
        toc=toc,
        file_hash=hashlib.sha256(data).hexdigest(),
        file_size=len(data),
        source_path=str(source),
        file_name=source.name,
    )


def parse_fb2_file(path: str | Path) -> ParsedBook:
    """Прочитать и разобрать FB2-файл с диска."""
    file_path = Path(path)
    try:
        data = file_path.read_bytes()
    except OSError as exc:
        raise Fb2Error(f"не удалось прочитать файл: {exc}") from exc
    return parse_fb2_bytes(data, file_path)


def iter_fb2_files(
    root: str | Path,
    *,
    recursive: bool = True,
    skip_dirs: Iterable[str] = (".git", ".svn", ".hg", "__pycache__", ".venv", "node_modules"),
) -> Iterable[Path]:
    """Найти файлы .fb2 и .fb2.zip в каталоге."""
    root_path = Path(root)
    if not root_path.is_dir():
        return
    skip = {name.casefold() for name in skip_dirs}

    if recursive:
        candidates = root_path.rglob("*")
    else:
        candidates = root_path.iterdir()

    for path in candidates:
        try:
            if not path.is_file():
                continue
        except OSError:
            continue
        if any(part.casefold() in skip for part in path.relative_to(root_path).parts[:-1]):
            continue
        lower = path.name.lower()
        if lower.endswith(".fb2") or lower.endswith(".fb2.zip"):
            yield path
