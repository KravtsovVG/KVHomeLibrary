"""Безопасный рендеринг HTML-описаний из внешних OPDS.

Описания приходят из недоверенных источников, поэтому HTML пропускается
через allowlist: разрешены только простые теги форматирования, ссылки и
картинки. Скрипты, стили, iframe, обработчики событий и `javascript:`
удаляются.
"""

from __future__ import annotations

import html
import re
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

from .templating import render

_ALLOWED_TAGS = {
    "p",
    "br",
    "b",
    "strong",
    "i",
    "em",
    "u",
    "s",
    "strike",
    "del",
    "ins",
    "blockquote",
    "ul",
    "ol",
    "li",
    "a",
    "img",
    "code",
    "pre",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "hr",
    "span",
    "div",
    "sub",
    "sup",
    "small",
    "table",
    "thead",
    "tbody",
    "tr",
    "th",
    "td",
}
_VOID_TAGS = {"br", "img", "hr"}
_DROP_CONTENT = {
    "script",
    "style",
    "iframe",
    "object",
    "embed",
    "svg",
    "math",
    "template",
    "form",
    "input",
    "button",
    "select",
    "textarea",
    "link",
    "meta",
    "base",
}
_ALLOWED_ATTRS = {
    "a": {"href", "title"},
    "img": {"src", "alt", "title", "width", "height"},
    "td": {"colspan", "rowspan"},
    "th": {"colspan", "rowspan"},
}
_TAG_RE = re.compile(r"<[^>]+>")
_SPACE_RE = re.compile(r"\s+")


def _local_tag(tag: str) -> str:
    return tag.lower().split(":", 1)[-1]


class _Sanitizer(HTMLParser):
    def __init__(self, base_url: str = "") -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.out: list[str] = []
        self.stack: list[str] = []
        self.drop_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = _local_tag(tag)
        if self.drop_depth:
            if tag in _DROP_CONTENT:
                self.drop_depth += 1
            return
        if tag in _DROP_CONTENT:
            if tag not in _VOID_TAGS:
                self.drop_depth = 1
            return
        if tag not in _ALLOWED_TAGS:
            return
        self.out.append(self._open_tag(tag, attrs))
        if tag not in _VOID_TAGS:
            self.stack.append(tag)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = _local_tag(tag)
        if self.drop_depth or tag in _DROP_CONTENT or tag not in _ALLOWED_TAGS:
            return
        self.out.append(self._open_tag(tag, attrs))

    def handle_endtag(self, tag: str) -> None:
        tag = _local_tag(tag)
        if self.drop_depth:
            if tag in _DROP_CONTENT:
                self.drop_depth -= 1
            return
        if tag not in _ALLOWED_TAGS:
            return
        if tag not in self.stack:
            return
        while self.stack:
            current = self.stack.pop()
            self.out.append(render("partials/safe_close.html", tag=current))
            if current == tag:
                break

    def handle_data(self, data: str) -> None:
        if self.drop_depth or not data:
            return
        self.out.append(html.escape(data, quote=False))

    def handle_comment(self, data: str) -> None:
        return

    def html(self) -> str:
        while self.stack:
            self.out.append(render("partials/safe_close.html", tag=self.stack.pop()))
        return "".join(self.out)

    def _open_tag(self, tag: str, attrs: list[tuple[str, str | None]]) -> str:
        allowed = _ALLOWED_ATTRS.get(tag, set())
        rendered: list[dict[str, str]] = []
        for key, value in attrs:
            key = (key or "").lower()
            value = (value or "").strip()
            if key not in allowed or not value:
                continue
            if key in {"href", "src"}:
                value = self._safe_url(value, image=(key == "src"))
                if not value:
                    continue
            elif key in {"width", "height", "colspan", "rowspan"}:
                if not value.isdigit():
                    continue
                if key in {"colspan", "rowspan"} and not 1 <= int(value) <= 20:
                    continue
            rendered.append({"name": key, "value": value})

        if tag == "a" and any(item["name"] == "href" for item in rendered):
            rendered.append({"name": "target", "value": "_blank"})
            rendered.append({"name": "rel", "value": "noopener noreferrer"})
        if tag == "img":
            rendered.append({"name": "loading", "value": "lazy"})
            rendered.append({"name": "referrerpolicy", "value": "no-referrer"})

        return render("partials/safe_tag.html", tag=tag, attrs=rendered)

    def _safe_url(self, value: str, *, image: bool) -> str:
        value = html.unescape(value).strip()
        if not value:
            return ""
        absolute = urljoin(self.base_url, value) if self.base_url else value
        parsed = urlparse(absolute)
        allowed = {"http", "https"} if image else {"http", "https", "mailto"}
        if parsed.scheme not in allowed:
            return ""
        return absolute


def sanitize_html(fragment: str, base_url: str = "") -> str:
    """Очистить HTML-описание, сохранив базовое форматирование."""
    if not fragment:
        return ""
    parser = _Sanitizer(base_url)
    try:
        parser.feed(fragment[:200_000])
        parser.close()
    except Exception:
        return ""
    return parser.html()


def html_to_text(fragment: str) -> str:
    """Плоский текст из HTML-описания (для поиска/фолбэка)."""
    safe = sanitize_html(fragment)
    text = html.unescape(_TAG_RE.sub(" ", safe))
    return _SPACE_RE.sub(" ", text).strip()
