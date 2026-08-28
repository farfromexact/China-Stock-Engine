from __future__ import annotations

from pathlib import Path
import shutil
import unittest

import pandas as pd

from china_stock_engine.storage import (
    ArtifactContractError,
    MAX_OPPORTUNITY_RADAR_BYTES,
    atomic_write_json,
    load_manifest,
    publish_data_reference_artifacts,
)


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

    def test_compact_json_is_deterministic_and_multiline(self) -> None:
        path = TEST_ROOT / "compact.json"
        atomic_write_json(path, {"z": [1, 2], "a": "值"}, compact=True)
        text = path.read_text(encoding="utf-8")
        self.assertEqual(
            text,
            '{\n "a":"值",\n "z":[\n  1,\n  2\n ]\n}\n',
        )
        self.assertGreater(len(text.splitlines()), 1)

    def test_oversize_radar_fails_before_snapshot_or_latest_is_touched(self) -> None:
        data_dir = TEST_ROOT / "data"
        snapshot = data_dir / "snapshots" / "2026-08-28"
        latest = data_dir / "latest"
        atomic_write_json(
            snapshot / "manifest.json",
            {
                "schema_version": 3,
                "trade_date": "2026-08-28",
                "artifacts": {"sentinel.json": {"sha256": "unused"}},
            },
        )
        atomic_write_json(latest / "manifest.json", {"sentinel": "last-valid"})
        latest_before = (latest / "manifest.json").read_bytes()

        with self.assertRaisesRegex(ArtifactContractError, "hard limit"):
            publish_data_reference_artifacts(
                data_dir,
                "2026-08-28",
                pd.DataFrame({"thscode": ["600000.SH"]}),
                {},
                {},
                {
                    "candidate_union": [],
                    "oversize": "x" * (MAX_OPPORTUNITY_RADAR_BYTES + 1),
                },
                {},
                publish_latest=False,
            )

        self.assertEqual(latest_before, (latest / "manifest.json").read_bytes())
        self.assertFalse((snapshot / "stock_state.parquet").exists())
        self.assertFalse((snapshot / "opportunity_radar_latest.json").exists())


if __name__ == "__main__":
    unittest.main()
