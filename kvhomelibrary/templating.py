"""Минимальный шаблонизатор без сторонних зависимостей.

Поддерживает переменные, условия, циклы и include. Этого достаточно для
всех страниц проекта, а HTML полностью живёт в каталоге ``templates``.
"""

from __future__ import annotations

import html
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"

_TOKEN_RE = re.compile(r"({{.*?}}|{%.*?%}|{#.*?#})", re.S)
_NAME_RE = re.compile(r"^[A-Za-z0-9_./-]+\.html$")
_FOR_RE = re.compile(r"^for\s+([A-Za-z_][A-Za-z0-9_]*)\s+in\s+(.+)$")
_COMPARE_RE = re.compile(r"^(.*?)\s*(==|!=)\s*(.*)$")


class TemplateError(RuntimeError):
    pass


class SafeString(str):
    """Строка, которую не нужно HTML-экранировать."""


def mark_safe(value: object) -> SafeString:
    return SafeString("" if value is None else str(value))


def _validate_name(name: str) -> str:
    name = (name or "").strip()
    if not _NAME_RE.match(name) or ".." in name or name.startswith("/"):
        raise TemplateError(f"некорректное имя шаблона: {name!r}")
    return name


def _resolve(expr: str, context: Mapping[str, Any]) -> Any:
    expr = expr.strip()
    if not expr:
        return ""
    if (expr.startswith('"') and expr.endswith('"')) or (
        expr.startswith("'") and expr.endswith("'")
    ):
        return expr[1:-1]
    if expr in {"true", "True"}:
        return True
    if expr in {"false", "False"}:
        return False
    if expr in {"none", "None", "null"}:
        return None
    if expr.isdigit():
        return int(expr)
    parts = expr.split(".")
    value: Any = context.get(parts[0], "")
    for part in parts[1:]:
        if isinstance(value, Mapping):
            value = value.get(part, "")
        else:
            value = getattr(value, part, "")
    return value


def _condition(expr: str, context: Mapping[str, Any]) -> bool:
    expr = expr.strip()
    if expr.startswith("not "):
        return not _condition(expr[4:], context)
    match = _COMPARE_RE.match(expr)
    if match:
        left = _resolve(match.group(1), context)
        right = _resolve(match.group(3), context)
        if match.group(2) == "==":
            return left == right
        return left != right
    return bool(_resolve(expr, context))


def _render_value(expr: str, context: Mapping[str, Any]) -> str:
    parts = [part.strip() for part in expr.split("|")]
    value = _resolve(parts[0], context)
    safe = any(part == "safe" for part in parts[1:])
    if isinstance(value, SafeString) or safe:
        return "" if value is None else str(value)
    return html.escape("" if value is None else str(value), quote=True)


def _parse(
    tokens: list[str],
    index: int = 0,
    stops: frozenset[str] = frozenset(),
) -> tuple[list[tuple[Any, ...]], int, str]:
    nodes: list[tuple[Any, ...]] = []
    while index < len(tokens):
        token = tokens[index]
        if token.startswith("{%"):
            command = token[2:-2].strip()
            if command in stops:
                return nodes, index, command
            if command.startswith("if "):
                condition = command[3:].strip()
                body, end, stop = _parse(tokens, index + 1, frozenset({"else", "endif"}))
                other: list[tuple[Any, ...]] = []
                if stop == "else":
                    other, end, stop = _parse(tokens, end + 1, frozenset({"endif"}))
                if stop != "endif":
                    raise TemplateError("не закрыт блок if")
                nodes.append(("if", condition, body, other))
                index = end + 1
            elif command.startswith("for "):
                match = _FOR_RE.match(command)
                if not match:
                    raise TemplateError(f"некорректный for: {command}")
                body, end, stop = _parse(tokens, index + 1, frozenset({"endfor"}))
                if stop != "endfor":
                    raise TemplateError("не закрыт блок for")
                nodes.append(("for", match.group(1), match.group(2).strip(), body))
                index = end + 1
            elif command.startswith("include "):
                name = command[8:].strip().strip('"\'')
                nodes.append(("include", _validate_name(name)))
                index += 1
            else:
                raise TemplateError(f"неизвестная команда шаблона: {command}")
        elif token.startswith("{{"):
            nodes.append(("expr", token[2:-2].strip()))
            index += 1
        elif token.startswith("{#"):
            index += 1
        else:
            nodes.append(("text", token))
            index += 1
    return nodes, index, ""


@lru_cache(maxsize=128)
def _load(name: str) -> list[tuple[Any, ...]]:
    name = _validate_name(name)
    base = TEMPLATES_DIR.resolve()
    path = (base / name).resolve()
    if base not in path.parents:
        raise TemplateError(f"шаблон вне каталога templates: {name}")
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise TemplateError(f"не найден шаблон {name}: {exc}") from exc
    tokens = _TOKEN_RE.split(text)
    nodes, _, _ = _parse(tokens)
    return nodes


def _render_nodes(
    nodes: list[tuple[Any, ...]],
    context: Mapping[str, Any],
    out: list[str],
) -> None:
    for node in nodes:
        kind = node[0]
        if kind == "text":
            out.append(node[1])
        elif kind == "expr":
            out.append(_render_value(node[1], context))
        elif kind == "if":
            branch = node[2] if _condition(node[1], context) else node[3]
            _render_nodes(branch, context, out)
        elif kind == "for":
            variable, expr, body = node[1], node[2], node[3]
            values = _resolve(expr, context)
            if values is None:
                continue
            try:
                iterator = list(values)
            except TypeError:
                continue
            for position, item in enumerate(iterator):
                local = dict(context)
                local[variable] = item
                local["loop"] = {
                    "index": position + 1,
                    "index0": position,
                    "first": position == 0,
                    "last": position == len(iterator) - 1,
                }
                _render_nodes(body, local, out)
        elif kind == "include":
            _render_nodes(_load(node[1]), context, out)


def render(name: str, **context: Any) -> str:
    out: list[str] = []
    _render_nodes(_load(_validate_name(name)), context, out)
    return "".join(out)
