from __future__ import annotations

import io
import unittest
import zipfile
from pathlib import Path

from kvhomelibrary.fb2 import (
    Fb2Error,
    iter_fb2_files,
    parse_fb2_bytes,
    parse_fb2_file,
)
from tests.support import WorkspaceTempDir

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "examples" / "books"
BULGAKOV = EXAMPLES / "bulgakov-belaya-gvardiya.fb2"
LUKYANENKO = EXAMPLES / "lukyanenko-nochnoy-dozor.fb2"
STRUGATSKIE = EXAMPLES / "strugatskie-piknik-na-obochine.fb2"


class Fb2ParsingTests(unittest.TestCase):
    def test_full_metadata(self) -> None:
        book = parse_fb2_file(BULGAKOV)
        self.assertEqual(book.title, "Белая гвардия")
        self.assertEqual(len(book.authors), 1)
        self.assertEqual(book.authors[0].last_name, "Булгаков")
        self.assertEqual(book.authors[0].display_name, "Михаил Афанасьевич Булгаков")
        self.assertEqual(book.genres, ["prose_classic"])
        self.assertEqual(book.series, "Русская классика")
        self.assertEqual(book.series_number, "1")
        self.assertEqual(book.publisher, "Недра")
        self.assertEqual(book.city, "Москва")
        self.assertEqual(book.publish_year, "1925")
        self.assertEqual(book.isbn, "978-5-00000-001-1")
        self.assertEqual(book.lang, "ru")
        self.assertEqual(book.doc_id, "BULGAKOV-BELAYA-GVARDIYA")
        self.assertIn("Турбиных", book.annotation)
        self.assertIn("Гражданская война", book.keywords)
        self.assertEqual(book.cover_mime, "image/png")
        self.assertEqual(book.cover_ext, "png")
        self.assertIsNotNone(book.cover_data)
        self.assertGreater(len(book.cover_data or b""), 20)
        self.assertEqual(len(book.file_hash), 64)
        self.assertEqual(book.file_size, BULGAKOV.stat().st_size)
        self.assertIn("<section", book.content_html)
        self.assertIn("<h2>Глава первая</h2>", book.content_html)
        self.assertIn("Велик был год", book.content_html)
        self.assertTrue(book.toc)
        self.assertEqual(book.toc[0]["title"], "Глава первая")

    def test_multiple_authors_and_no_cover(self) -> None:
        book = parse_fb2_file(STRUGATSKIE)
        self.assertEqual(len(book.authors), 2)
        self.assertEqual(book.authors[0].last_name, "Стругацкий")
        self.assertEqual(book.authors[1].first_name, "Борис")
        self.assertEqual(book.genres, ["sf", "sf_social"])
        self.assertEqual(book.series, "")
        self.assertIsNone(book.cover_data)
        self.assertIn("Зона не любит", book.content_html)

    def test_zip_archive(self) -> None:
        data = BULGAKOV.read_bytes()
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("book.fb2", data)
        book = parse_fb2_bytes(buffer.getvalue(), "archive.fb2.zip")
        self.assertEqual(book.title, "Белая гвардия")
        self.assertEqual(book.file_name, "archive.fb2.zip")

    def test_windows_1251_encoding(self) -> None:
        xml = """<?xml version="1.0" encoding="windows-1251"?>
<FictionBook xmlns="http://www.gribuser.ru/xml/fictionbook/2.0">
  <description>
    <title-info>
      <genre>prose</genre>
      <author><first-name>Иван</first-name><last-name>Тестов</last-name></author>
      <book-title>Книга в кодировке Windows-1251</book-title>
      <lang>ru</lang>
    </title-info>
  </description>
  <body><section><p>Текст</p></section></body>
</FictionBook>
"""
        book = parse_fb2_bytes(xml.encode("cp1251"), "cp1251.fb2")
        self.assertEqual(book.title, "Книга в кодировке Windows-1251")
        self.assertEqual(book.authors[0].last_name, "Тестов")

    def test_broken_xml_raises(self) -> None:
        with self.assertRaises(Fb2Error):
            parse_fb2_bytes(b"<FictionBook><description>", "broken.fb2")

    def test_not_fb2_raises(self) -> None:
        with self.assertRaises(Fb2Error):
            parse_fb2_bytes(b"<?xml version='1.0'?><html/>", "not-fb2.fb2")

    def test_iter_files_recursive(self) -> None:
        with WorkspaceTempDir() as root:
            (root / "sub").mkdir()
            (root / "a.fb2").write_bytes(b"x")
            (root / "sub" / "b.fb2.zip").write_bytes(b"x")
            (root / "sub" / "c.txt").write_text("x")
            (root / ".git").mkdir()
            (root / ".git" / "ignored.fb2").write_bytes(b"x")

            recursive = list(iter_fb2_files(root))
            self.assertEqual({path.name for path in recursive}, {"a.fb2", "b.fb2.zip"})

            flat = list(iter_fb2_files(root, recursive=False))
            self.assertEqual({path.name for path in flat}, {"a.fb2"})


if __name__ == "__main__":
    unittest.main()
