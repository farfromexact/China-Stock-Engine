from __future__ import annotations

from pathlib import Path
import shutil
import unittest

from china_stock_engine.storage import load_manifest


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


if __name__ == "__main__":
    unittest.main()
