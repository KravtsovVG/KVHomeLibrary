"""Дерево категорий по именам и вспомогательные функции.

Категория ``Основная_Подкатегория`` относится к корню ``Основная`` и
подкатегории ``Подкатегория``. Разделитель — первое вхождение ``_``;
остаток имени сохраняется как название подкатегории.
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


def build_category_tree(genres: list[GenreRef]) -> list[CategoryNode]:
    """Построить двухуровневое дерево из плоского списка категорий."""
    roots: dict[str, CategoryNode] = {}
    for genre in genres:
        name = (genre.name or "").strip()
        if not name:
            continue
        root_name, _child = split_category(name)
        if not root_name:
            continue
        key = root_name.casefold()
        root = roots.get(key)
        if root is None:
            root = CategoryNode(
                name=root_name,
                label=root_name,
                sort_key=root_name.casefold(),
            )
            roots[key] = root

        if "_" not in name or name == root_name:
            # Категория верхнего уровня без подкатегории.
            root.genre_id = genre.id
            root.own_books += int(genre.books or 0)
            root.books += int(genre.books or 0)
            continue

        child = CategoryNode(
            name=name,
            label=name.split("_", 1)[1].strip() or name,
            genre_id=genre.id,
            books=int(genre.books or 0),
            own_books=int(genre.books or 0),
            sort_key=name.casefold(),
        )
        root.children.append(child)
        root.books += child.books

    for root in roots.values():
        root.children.sort(key=lambda item: (item.sort_key, item.name))
        root.sort_key = (root.label.casefold(), root.name)
    return sorted(roots.values(), key=lambda item: (item.sort_key, item.name))


def flatten_tree(nodes: list[CategoryNode]) -> list[dict[str, object]]:
    """Плоский список для select и компактного вывода."""
    result: list[dict[str, object]] = []
    for root in nodes:
        result.append(
            {
                "name": root.name,
                "label": root.label,
                "genre_id": root.genre_id,
                "books": root.books,
                "depth": 0,
                "url": web_url(root),
                "opds_url": opds_url(root),
            }
        )
        for child in root.children:
            result.append(
                {
                    "name": child.name,
                    "label": child.label,
                    "genre_id": child.genre_id,
                    "books": child.books,
                    "depth": 1,
                    "url": web_url(child),
                    "opds_url": opds_url(child),
                }
            )
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
    return None


def category_label(name: str) -> str:
    root, child = split_category(name)
    return f"{root} → {child}" if child else root
