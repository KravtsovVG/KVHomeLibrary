"""Метаданные и полезная нагрузка книг разных форматов.

FB2 разбирается в :mod:`kvhomelibrary.fb2`; EPUB читается как ZIP/OPF,
для PDF извлекаются базовые поля Info, для DjVu используется имя файла.
Модуль не зависит от сторонних библиотек.
"""

from __future__ import annotations

import hashlib
import io
import re
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

from .fb2 import Fb2Author, ParsedBook, _clean, parse_fb2_bytes

FORMATS = ("fb2", "pdf", "djvu", "epub")
FORMAT_LABELS = {
    "fb2": "FB2",
    "pdf": "PDF",
    "djvu": "DjVu",
    "epub": "EPUB",
}
FORMAT_MIME = {
    "fb2": "application/fb2+xml",
    "pdf": "application/pdf",
    "djvu": "image/vnd.djvu",
    "epub": "application/epub+zip",
}
_TAG_RE = re.compile(r"<[^>]+>")
_SPACE_RE = re.compile(r"\s+")


def format_mime(fmt: str) -> str:
    return FORMAT_MIME.get((fmt or "").casefold(), "application/octet-stream")


def format_label(fmt: str) -> str:
    return FORMAT_LABELS.get((fmt or "").casefold(), (fmt or "файл").upper())


def sort_key(title: str) -> str:
    value = _SPACE_RE.sub(" ", (title or "").casefold()).strip()
    value = re.sub(r"^(?:the|a|an)\s+", "", value)
    return re.sub(r"[«»\"'`]", "", value).strip()


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _text(value: str | None) -> str:
    return _SPACE_RE.sub(" ", _TAG_RE.sub(" ", value or "")).strip()


def _xml(data: bytes) -> ET.Element | None:
    try:
        return ET.fromstring(data)
    except ET.ParseError:
        return None


def _epub_cover(archive: zipfile.ZipFile, opf_path: str, root: ET.Element) -> tuple[bytes | None, str, str]:
    base = Path(opf_path).parent
    manifest = None
    for item in root.iter():
        if _local(item.tag) == "manifest":
            manifest = item
            break
    if manifest is None:
        return None, "", ""
    for item in manifest:
        if _local(item.tag) != "item":
            continue
        props = (item.get("properties") or "").casefold()
        item_id = (item.get("id") or "").casefold()
        if "cover-image" not in props and item_id != "cover-image":
            continue
        href = item.get("href") or ""
        media_type = item.get("media-type") or ""
        if not href:
            continue
        member = str((base / href).as_posix())
        try:
            data = archive.read(member)
        except KeyError:
            continue
        suffix = Path(href).suffix.lower().lstrip(".") or "jpg"
        return data, media_type or "image/jpeg", suffix
    return None, "", ""


def parse_epub(data: bytes, file_name: str) -> ParsedBook:
    title = Path(file_name).stem
    authors: list[Fb2Author] = []
    annotation = ""
    lang = ""
    isbn = ""
    cover_data: bytes | None = None
    cover_mime = ""
    cover_ext = ""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            container = _xml(archive.read("META-INF/container.xml"))
            opf_path = ""
            if container is not None:
                for elem in container.iter():
                    if _local(elem.tag) == "rootfile" and elem.get("full-path"):
                        opf_path = elem.get("full-path") or ""
                        break
            if not opf_path:
                names = [name for name in archive.namelist() if name.lower().endswith(".opf")]
                opf_path = names[0] if names else ""
            if opf_path:
                root = _xml(archive.read(opf_path))
                if root is not None:
                    creators: list[str] = []
                    for elem in root.iter():
                        tag = _local(elem.tag)
                        text = _clean(elem.text or "")
                        if tag == "title" and title == Path(file_name).stem:
                            title = text or title
                        elif tag == "creator" and text:
                            creators.append(text)
                        elif tag == "description" and not annotation:
                            annotation = _text(elem.text or "")
                        elif tag == "language" and not lang:
                            lang = text
                        elif tag == "identifier" and not isbn:
                            candidate = text.removeprefix("urn:isbn:").replace("-", "")
                            if candidate.isdigit() and len(candidate) in {10, 13}:
                                isbn = candidate
                    authors = [Fb2Author(nickname=name) for name in creators]
                    cover_data, cover_mime, cover_ext = _epub_cover(archive, opf_path, root)
    except (KeyError, zipfile.BadZipFile, OSError):
        pass

    return ParsedBook(
        title=title,
        sort_key=sort_key(title),
        authors=authors,
        annotation=annotation,
        lang=lang,
        isbn=isbn,
        cover_data=cover_data,
        cover_mime=cover_mime,
        cover_ext=cover_ext,
        file_hash=hashlib.sha256(data).hexdigest(),
        file_size=len(data),
        file_name=Path(file_name).name,
        format="epub",
    )


def _pdf_string(value: str) -> str:
    if value.startswith("(") and value.endswith(")"):
        value = value[1:-1]
    if value.startswith("<") and value.endswith(">"):
        try:
            raw = bytes.fromhex(re.sub(r"\s+", "", value[1:-1]))
            return raw.decode("utf-16-be", errors="ignore").strip("\x00")
        except ValueError:
            return ""
    value = re.sub(r"\\([nrtbf()\\])", lambda m: {"n": "\n", "r": "\r", "t": "\t", "b": "\b", "f": "\f"}.get(m.group(1), m.group(1)), value)
    return value.replace("\\(", "(").replace("\\)", ")").replace("\\\\", "\\")


def parse_pdf(data: bytes, file_name: str) -> ParsedBook:
    head = data[:4_000_000].decode("latin-1", errors="ignore")
    title = ""
    author = ""
    match = re.search(r"/Title\s*(\((?:[^()\\]|\\.)*\)|<[0-9A-Fa-f\s]+>)", head, re.S)
    if match:
        title = _clean(_pdf_string(match.group(1)))
    match = re.search(r"/Author\s*(\((?:[^()\\]|\\.)*\)|<[0-9A-Fa-f\s]+>)", head, re.S)
    if match:
        author = _clean(_pdf_string(match.group(1)))
    if not title:
        title = Path(file_name).stem
    authors = [Fb2Author(nickname=author)] if author else []
    return ParsedBook(
        title=title,
        sort_key=sort_key(title),
        authors=authors,
        file_hash=hashlib.sha256(data).hexdigest(),
        file_size=len(data),
        file_name=Path(file_name).name,
        format="pdf",
    )


def parse_djvu(data: bytes, file_name: str) -> ParsedBook:
    title = Path(file_name).stem
    return ParsedBook(
        title=title,
        sort_key=sort_key(title),
        file_hash=hashlib.sha256(data).hexdigest(),
        file_size=len(data),
        file_name=Path(file_name).name,
        format="djvu",
    )


def build_book(data: bytes, file_name: str, fmt: str, source_path: str) -> ParsedBook:
    fmt = (fmt or "").casefold()
    if fmt == "fb2":
        book = parse_fb2_bytes(data, source_path=source_path)
    elif fmt == "epub":
        book = parse_epub(data, file_name)
    elif fmt == "pdf":
        book = parse_pdf(data, file_name)
    else:
        book = parse_djvu(data, file_name)

    book.format = fmt
    book.file_name = Path(file_name).name
    book.source_path = source_path
    book.file_hash = hashlib.sha256(data).hexdigest()
    book.file_size = len(data)
    return book


def read_book_payload(path: str | Path, fmt: str, preferred_name: str = "") -> tuple[bytes, str, str]:
    """Вернуть (данные, имя файла, MIME) из обычного файла или ZIP-архива."""
    file_path = Path(path)
    fmt = (fmt or "fb2").casefold()
    mime = format_mime(fmt)
    if not zipfile.is_zipfile(file_path):
        return file_path.read_bytes(), preferred_name or file_path.name, mime

    with zipfile.ZipFile(file_path) as archive:
        names = [
            name for name in archive.namelist()
            if not name.endswith("/") and not Path(name).name.startswith(".")
        ]
        candidates = [name for name in names if Path(name).suffix.casefold().lstrip(".") == fmt]
        selected = min(candidates or names, key=lambda item: (item.count("/"), item.lower())) if names else ""
        if not selected:
            raise ValueError("в ZIP-архиве нет файлов")
        data = archive.read(selected)
        inner_name = Path(selected).name
        if preferred_name and not preferred_name.casefold().endswith(".zip"):
            inner_name = preferred_name
        return data, inner_name, mime
