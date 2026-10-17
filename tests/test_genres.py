from __future__ import annotations

import unittest

from kvhomelibrary.db import (
    connect_db,
    init_db,
    list_genres,
    normalize_genres,
    upsert_book,
)
from kvhomelibrary.fb2 import parse_fb2_file
from kvhomelibrary.genres import genre_name, genre_synonyms, normalize_genre
from tests.support import WorkspaceTempDir

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BULGAKOV = ROOT / "examples" / "books" / "bulgakov-belaya-gvardiya.fb2"

# Коды, которые реально встречаются в библиотеках LitRes/Flibusta.
REAL_CODES = {
    "network_literature": "Сетевая литература",
    "sf_litrpg": "ЛитРПГ",
    "litrpg": "ЛитРПГ",
    "popadanec": "Попаданцы",
    "fantasy_fight": "Боевое фэнтези",
    "comp_db": "Базы данных",
    "antique": "Античная литература",
    "comp_programming": "Программирование",
    "home_diy": "Своими руками",
    "computers": "Компьютеры",
    "fanfiction": "Фанфикшн",
    "sf_technofantasy": "Технофэнтези",
    "unrecognised": "Без категории",
    "comp_www": "Интернет и веб",
    "unfinished": "Незавершённое",
    "det_action": "Боевой детектив",
    "russian_fantasy": "Русское фэнтези",
    "city_fantasy": "Городское фэнтези",
    "popular_business": "Популярный бизнес",
    "economics_ref": "Экономика: справочники",
    "banking": "Банковское дело",
    "russian_contemporary": "Современная русская проза",
    "comp_osnet": "Операционные системы и сети",
    "reference": "Справочная литература",
    "urban-fantasy": "Городское фэнтези",
    "samizdat": "Самиздат",
    "great_story": "Крупная форма",
    "foreign_home": "Зарубежная прикладная литература",
    "foreign_fantasy": "Зарубежное фэнтези",
    "science": "Наука",
    "sf_space_opera": "Космическая опера",
    "love_hard": "Жёсткий любовный роман",
    "historical_fantasy": "Историческое фэнтези",
    "vampire_book": "Вампирское фэнтези",
    "dragon_fantasy": "Фэнтези о драконах",
    "sf_stimpank": "Стимпанк",
    "det_espionage": "Шпионский детектив",
    "beginning_authors": "Начинающие авторы",
    "comp_hard": "Аппаратное обеспечение",
    "sci_business": "Бизнес",
    "sci_juris": "Право",
    "foreign_comp": "Зарубежная компьютерная литература",
    "humor_fantasy": "Юмористическое фэнтези",
    "aphorism_quote": "Афоризмы",
    "popadantsy-v-magicheskie-miry": "Попаданцы",
    "foreign_desc": "Зарубежная литература о природе",
    "paranormal": "Паранормальные явления",
    "sci_history": "История",
    "sci_radio": "Радиотехника",
    "sci_popular": "Научно-популярная литература",
    "home_sport": "Спорт",
    "child_adv": "Детские приключения",
    "sf_fantasy_irony": "Ироническое фэнтези",
    "sf_irony": "Ироническая фантастика",
    "religion_self": "Религия и саморазвитие",
    "psy_generic": "Психология",
    "nonfiction": "Документальная литература",
    "comp_soft": "Программное обеспечение",
    "marketing": "Маркетинг",
    "child_education": "Детская познавательная литература",
    "child_sf": "Детская фантастика",
    "sf_litRPG": "ЛитРПГ",
    "ЛитРПГ": "ЛитРПГ",
    "ЛИТРПГ": "ЛитРПГ",
    "Фантастика": "Фантастика",
    "Фэнтези": "Фэнтези",
    "Киберпанк": "Киберпанк",
    "Эротика": "Эротика",
}


class GenreLocalizationTests(unittest.TestCase):
    def test_real_codes_have_russian_names(self) -> None:
        for code, expected in REAL_CODES.items():
            with self.subTest(code=code):
                self.assertEqual(genre_name(code), expected)

    def test_aliases_are_normalized(self) -> None:
        self.assertEqual(normalize_genre("sf-action"), "sf_action")
        self.assertEqual(normalize_genre("sf-history"), "sf_history")
        self.assertEqual(normalize_genre("Боевая фантастика"), "sf_action")
        self.assertEqual(normalize_genre("sf_litRPG"), "litrpg")
        self.assertEqual(normalize_genre("urban-fantasy"), "sf_fantasy_city")
        self.assertEqual(normalize_genre("nonfiction"), "nonf")

    def test_synonyms(self) -> None:
        self.assertIn("фантастическая литература", genre_synonyms("sf"))
        self.assertIn("сетература", genre_synonyms("network_literature"))
        self.assertIn("попаданчество", genre_synonyms("popadanec"))
        self.assertNotIn("Фантастика", genre_synonyms("sf"))

    def test_migration_merges_duplicate_categories(self) -> None:
        with WorkspaceTempDir() as tmp:
            db = tmp / "library.db"
            init_db(db)
            conn = connect_db(db)
            try:
                book = parse_fb2_file(BULGAKOV)
                book.genres = ["sf_fantasy"]
                book_id, _ = upsert_book(conn, book, covers_dir=tmp / "covers")

                conn.execute(
                    "INSERT INTO genres(code, name, sort_key) VALUES (?, ?, ?)",
                    ("sf_action", "sf_action", "sf_action"),
                )
                canonical_id = conn.execute(
                    "SELECT id FROM genres WHERE code = 'sf_action'"
                ).fetchone()["id"]
                conn.execute(
                    "INSERT INTO genres(code, name, sort_key) VALUES (?, ?, ?)",
                    ("sf-action", "sf-action", "sf-action"),
                )
                alias_id = conn.execute(
                    "SELECT id FROM genres WHERE code = 'sf-action'"
                ).fetchone()["id"]
                conn.execute(
                    "INSERT INTO book_genres(book_id, genre_id) VALUES (?, ?)",
                    (book_id, alias_id),
                )
                conn.commit()

                changed = normalize_genres(conn)
                self.assertGreaterEqual(changed, 1)
                rows = conn.execute("SELECT code, name FROM genres").fetchall()
                codes = {row["code"] for row in rows}
                self.assertIn("sf_action", codes)
                self.assertNotIn("sf-action", codes)
                name = conn.execute(
                    "SELECT name FROM genres WHERE code = 'sf_action'"
                ).fetchone()["name"]
                self.assertEqual(name, "Боевая фантастика")
                link = conn.execute(
                    "SELECT genre_id FROM book_genres WHERE book_id = ? AND genre_id = ?",
                    (book_id, alias_id),
                ).fetchone()
                self.assertIsNone(link)
                moved = conn.execute(
                    "SELECT genre_id FROM book_genres WHERE book_id = ? AND genre_id = ?",
                    (book_id, canonical_id),
                ).fetchone()
                self.assertIsNotNone(moved)
            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main()
