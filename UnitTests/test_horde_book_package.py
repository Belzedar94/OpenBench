import tempfile
from pathlib import Path
import unittest
from zipfile import ZipFile

from Scripts import package_horde_book as MODULE


class HordeBookPackageTests(unittest.TestCase):
    def test_package_is_deterministic_and_preserves_payload(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.epd"
            payload = b"8/8/8/8/8/8/8/8 w - - 0 1;\n8/8/8/8/8/8/8/8 b - - 0 1;\n"
            source.write_bytes(payload)

            first = root / "first.zip"
            second = root / "second.zip"
            first_receipt = MODULE.package(source, first, "HORDE_openings.epd")
            second_receipt = MODULE.package(source, second, "HORDE_openings.epd")

            self.assertEqual(first.read_bytes(), second.read_bytes())
            self.assertEqual(first_receipt["records"], 2)
            self.assertEqual(
                first_receipt["archive_sha256"], second_receipt["archive_sha256"]
            )
            with ZipFile(first) as archive:
                self.assertEqual(archive.namelist(), ["HORDE_openings.epd"])
                self.assertEqual(archive.read("HORDE_openings.epd"), payload)
                self.assertEqual(
                    archive.getinfo("HORDE_openings.epd").date_time,
                    (1980, 1, 1, 0, 0, 0),
                )

    def test_rejects_invalid_payload_and_member(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.epd"
            source.write_text("missing terminator\n", encoding="ascii")
            with self.assertRaisesRegex(ValueError, "end with"):
                MODULE.package(source, root / "bad.zip", "HORDE_openings.epd")
            source.write_text("valid;\n", encoding="ascii")
            with self.assertRaisesRegex(ValueError, "plain .epd"):
                MODULE.package(source, root / "bad-member.zip", "sub/book.epd")


if __name__ == "__main__":
    unittest.main()
