from __future__ import annotations

import unittest

from kvhomelibrary.cli import build_parser


class OrganizeParserTests(unittest.TestCase):
    def test_scan_dir_option(self) -> None:
        args = build_parser().parse_args(
            ["organize", "--scan-dir", "D:/Books", "--db", "library.db"]
        )
        self.assertEqual(args.scan_dir, "D:/Books")
        self.assertIsNone(args.path)

    def test_source_alias(self) -> None:
        args = build_parser().parse_args(["organize", "--source", "D:/Books"])
        self.assertEqual(args.scan_dir, "D:/Books")

    def test_positional_path(self) -> None:
        args = build_parser().parse_args(["organize", "D:/Books"])
        self.assertEqual(args.path, "D:/Books")
        self.assertEqual(args.scan_dir, "")
