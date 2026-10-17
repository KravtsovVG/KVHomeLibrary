from __future__ import annotations

import unittest
from pathlib import Path

from kvhomelibrary import templating
from tests.support import WorkspaceTempDir


class TemplateEngineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = WorkspaceTempDir()
        self.addCleanup(self.tmp.cleanup)
        (self.tmp.path / "partials").mkdir()
        self.old_dir = templating.TEMPLATES_DIR
        templating.TEMPLATES_DIR = self.tmp.path
        templating._load.cache_clear()
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        templating.TEMPLATES_DIR = self.old_dir
        templating._load.cache_clear()

    def test_loop_if_escape_and_safe(self) -> None:
        (self.tmp.path / "page.html").write_text(
            "{% for item in items %}"
            "{% if item.ok %}<b>{{ item.name }}</b>{% else %}{{ item.name }}{% endif %}"
            "{% endfor %}",
            encoding="utf-8",
        )
        result = templating.render(
            "page.html",
            items=[{"name": "A", "ok": True}, {"name": "<B>", "ok": False}],
        )
        self.assertEqual(result, "<b>A</b>&lt;B&gt;")

    def test_safe_filter_and_include(self) -> None:
        (self.tmp.path / "partials" / "item.html").write_text(
            "<span>{{ value }}</span>", encoding="utf-8"
        )
        (self.tmp.path / "page.html").write_text(
            "{% include \"partials/item.html\" %}{{ raw|safe }}",
            encoding="utf-8",
        )
        result = templating.render(
            "page.html",
            value="<x>",
            raw="<p>ok</p>",
        )
        self.assertEqual(result, "<span>&lt;x&gt;</span><p>ok</p>")

    def test_rejects_path_traversal(self) -> None:
        with self.assertRaises(templating.TemplateError):
            templating.render("../secret.html")


if __name__ == "__main__":
    unittest.main()
