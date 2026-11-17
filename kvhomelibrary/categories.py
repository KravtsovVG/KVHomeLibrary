"""Дерево категорий и вспомогательные функции.

Путь категории хранится в ``category_path`` (например,
``Основная_Подкатегория``), а отображаемое имя — в ``display_name``.
Исторические категории без пути используют ``name``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import quote

from .db import GenreRef


@dataclass(slots=True)
class CategoryNode:
    name: str
    label: str
    genre_id: int | None = None
    books: int = 0
    own_books: int = 0
    sort_key: str = ""
    children: list["CategoryNode"] = field(default_factory=list)

    @property
    def is_root(self) -> bool:
        return self.genre_id is None or bool(self.children)


def split_category(name: str) -> tuple[str, str]:
    """Вернуть (корень, подкатегория) по первому ``_``."""
    value = (name or "").strip()
    if "_" not in value:
        return value, ""
    root, child = value.split("_", 1)
    root = root.strip()
    child = child.strip()
    if not root or not child:
        return value, ""
    return root, child


def category_root(name: str) -> str:
    return split_category(name)[0]


def _segments(path: str) -> list[str]:
    return [part.strip() for part in (path or "").split("_") if part.strip()]


def build_category_tree(genres: list[GenreRef]) -> list[CategoryNode]:
    """Построить дерево по путям категорий."""
    roots: dict[str, CategoryNode] = {}
    nodes: dict[str, CategoryNode] = {}

    for genre in genres:
        path = (genre.category_path or genre.name or "").strip()
        if not path:
            continue
        parent: CategoryNode | None = None
        prefix: list[str] = []
        parts = _segments(path)
        for position, segment in enumerate(parts):
            prefix.append(segment)
            full_name = "_".join(prefix)
            key = full_name.casefold()
            node = nodes.get(key)
            if node is None:
                node = CategoryNode(
                    name=full_name,
                    label=segment,
                    sort_key=full_name.casefold(),
                )
                nodes[key] = node
                if parent is None:
                    roots[key] = node
                else:
                    parent.children.append(node)
            if position == len(parts) - 1:
                node.genre_id = genre.id
                node.label = genre.display_name or segment
                node.own_books += int(genre.books or 0)
            node.books += int(genre.books or 0)
            parent = node

    def sort_children(items: list[CategoryNode]) -> None:
        for item in items:
            item.sort_key = (item.label.casefold(), item.name)
            sort_children(item.children)
        items.sort(key=lambda item: (item.sort_key, item.name))

    root_list = list(roots.values())
    sort_children(root_list)
    return root_list


def flatten_tree(nodes: list[CategoryNode]) -> list[dict[str, object]]:
    """Плоский список для select и компактного вывода."""
    result: list[dict[str, object]] = []

    def visit(node: CategoryNode, depth: int) -> None:
        result.append(
            {
                "name": node.name,
                "label": node.label,
                "genre_id": node.genre_id,
                "books": node.books,
                "depth": depth,
                "has_children": bool(node.children),
                "url": web_url(node),
                "opds_url": opds_url(node),
            }
        )
        for child in node.children:
            visit(child, depth + 1)

    for root in nodes:
        visit(root, 0)
    return result


def web_url(node: CategoryNode) -> str:
    if node.genre_id is not None and not node.children:
        return f"/ui/books?genre_id={node.genre_id}"
    return "/ui/books?category_root=" + quote(node.name, safe="")


def opds_url(node: CategoryNode) -> str:
    if node.genre_id is not None and not node.children:
        return f"/opds/genres/{node.genre_id}"
    return "/opds/genres/root/" + quote(node.name, safe="")


def find_root(nodes: list[CategoryNode], name: str) -> CategoryNode | None:
    key = (name or "").strip().casefold()
    for node in nodes:
        if node.name.casefold() == key:
            return node
        found = find_root(node.children, name)
        if found is not None:
            return found
    return None


def category_label(name: str) -> str:
    root, child = split_category(name)
    return f"{root} → {child}" if child else root
