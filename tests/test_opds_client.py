from __future__ import annotations

import unittest

from kvhomelibrary.opds_client import (
    Feed,
    FeedLink,
    OpdsError,
    fb2_link,
    is_navigation_link,
    parse_feed,
    search_url,
)

FEED = """<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"
      xmlns:dc="http://purl.org/dc/terms/"
      xmlns:opds="http://opds-spec.org/2010/catalog">
  <title>Внешний каталог</title>
  <id>urn:uuid:test-feed</id>
  <link rel="self" href="/opds" type="application/atom+xml;profile=opds-catalog;kind=navigation"/>
  <entry>
    <title>Авторы</title>
    <id>urn:uuid:authors</id>
    <link rel="subsection" href="authors/" type="application/atom+xml;profile=opds-catalog;kind=navigation"/>
  </entry>
  <entry>
    <title>Ночной Дозор</title>
    <id>urn:uuid:book-1</id>
    <author><name>Сергей Лукьяненко</name></author>
    <dc:language>ru</dc:language>
    <dc:issued>1998</dc:issued>
    <category term="sf_fantasy" label="Фэнтези"/>
    <content>Городское фэнтези.</content>
    <opds:series name="Дозоры" number="1"/>
    <link rel="http://opds-spec.org/image" href="../covers/1.jpg" type="image/jpeg"/>
    <link rel="http://opds-spec.org/acquisition" href="../books/1.fb2"
          type="application/fb2+xml" length="12345"/>
  </entry>
</feed>
""".encode("utf-8")


class OpdsClientTests(unittest.TestCase):
    def test_parse_navigation_and_acquisition(self) -> None:
        feed = parse_feed(FEED, "https://example.org/opds/")
        self.assertEqual(feed.title, "Внешний каталог")
        self.assertEqual(len(feed.entries), 2)
        self.assertEqual(feed.entries[0].navigation_link().href, "https://example.org/opds/authors/")  # type: ignore[union-attr]

        book = feed.entries[1]
        self.assertEqual(book.title, "Ночной Дозор")
        self.assertEqual(book.authors, ["Сергей Лукьяненко"])
        self.assertEqual(book.language, "ru")
        self.assertEqual(book.issued, "1998")
        self.assertEqual(book.categories, ["Фэнтези"])
        self.assertEqual(book.series, "Дозоры")
        self.assertEqual(book.series_number, "1")
        self.assertEqual(len(book.acquisition_links()), 1)
        link = fb2_link(book)
        self.assertIsNotNone(link)
        self.assertEqual(link.href, "https://example.org/books/1.fb2")  # type: ignore[union-attr]
        self.assertEqual(link.length, 12345)  # type: ignore[union-attr]

    def test_invalid_document(self) -> None:
        with self.assertRaises(OpdsError):
            parse_feed(b"<html/>", "https://example.org/opds")

    def test_navigation_link_variants(self) -> None:
        self.assertTrue(
            is_navigation_link(
                FeedLink(rel="", type="application/atom+xml", href="https://example.org/a")
            )
        )
        self.assertTrue(
            is_navigation_link(
                FeedLink(
                    rel="http://opds-spec.org/sort/new",
                    type="application/atom+xml",
                    href="https://example.org/new",
                )
            )
        )
        self.assertFalse(
            is_navigation_link(
                FeedLink(
                    rel="related",
                    type="application/atom+xml",
                    href="https://example.org/author/1",
                )
            )
        )

    def test_search_url_template(self) -> None:
        feed = Feed(
            title="source",
            links=[
                FeedLink(
                    rel="search",
                    href="https://example.org/opds/search?q={searchTerms}",
                    type="application/atom+xml",
                )
            ],
        )
        url = search_url(feed, "Дозоры")
        self.assertTrue(url.startswith("https://example.org/opds/search?q="))
        self.assertIn("%D0%94%D0%BE%D0%B7%D0%BE%D1%80%D1%8B", url)

    def test_html_description_is_sanitized(self) -> None:
        data = """<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>HTML feed</title>
  <entry>
    <title>Книга с HTML</title>
    <content type="html">&lt;p&gt;Описание &lt;b&gt;книги&lt;/b&gt;.&lt;/p&gt;
      &lt;script&gt;alert(1)&lt;/script&gt;
      &lt;a href="javascript:alert(1)"&gt;ссылка&lt;/a&gt;
      &lt;img src="../cover.jpg" onerror="alert(1)"&gt;
    </content>
  </entry>
</feed>""".encode("utf-8")
        feed = parse_feed(data, "https://example.org/catalog/opds")
        entry = feed.entries[0]
        self.assertIn("Описание", entry.summary_html)
        self.assertIn("<b>книги</b>", entry.summary_html)
        self.assertIn("<p>", entry.summary_html)
        self.assertNotIn("<script", entry.summary_html)
        self.assertNotIn("javascript", entry.summary_html)
        self.assertNotIn("onerror", entry.summary_html)
        self.assertIn('src="https://example.org/cover.jpg"', entry.summary_html)
        self.assertIn("Описание книги", entry.summary)

    def test_invalid_url(self) -> None:
        with self.assertRaises(OpdsError):
            parse_feed(FEED, "file:///tmp/opds.xml")


if __name__ == "__main__":
    unittest.main()
