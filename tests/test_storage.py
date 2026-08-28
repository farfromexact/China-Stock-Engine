from __future__ import annotations

from pathlib import Path
import shutil
import unittest

from china_stock_engine.storage import atomic_write_json, load_manifest


TEST_ROOT = Path(__file__).resolve().parents[1] / ".test-tmp" / "storage"


class StorageTests(unittest.TestCase):
    def setUp(self) -> None:
        if TEST_ROOT.exists():
            shutil.rmtree(TEST_ROOT)
        TEST_ROOT.mkdir(parents=True)

    def test_manifest_missing_is_missing_but_corruption_fails_closed(self) -> None:
        path = TEST_ROOT / "manifest.json"
        self.assertEqual(load_manifest(path), {})

        path.write_text("{broken", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "invalid JSON file"):
            load_manifest(path)

        path.write_text("[]", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "must contain an object"):
            load_manifest(path)

        path.write_text('{"schema_version": 999}', encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "unsupported manifest schema_version"):
            load_manifest(path)

    def test_compact_json_is_deterministic_and_has_no_pretty_print_padding(self) -> None:
        path = TEST_ROOT / "compact.json"
        atomic_write_json(path, {"z": [1, 2], "a": "值"}, compact=True)
        self.assertEqual(path.read_text(encoding="utf-8"), '{"a":"值","z":[1,2]}\n')


if __name__ == "__main__":
    unittest.main()
