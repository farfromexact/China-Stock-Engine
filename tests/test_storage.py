from __future__ import annotations

from pathlib import Path
import shutil
import unittest

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from china_stock_engine.storage import (
    ArtifactContractError,
    MAX_OPPORTUNITY_RADAR_BYTES,
    atomic_write_json,
    load_manifest,
    read_parquet_file,
    publish_data_reference_artifacts,
)


TEST_ROOT = Path(__file__).resolve().parents[1] / ".test-tmp" / "storage"


class StorageTests(unittest.TestCase):
    def setUp(self) -> None:
        if TEST_ROOT.exists():
            shutil.rmtree(TEST_ROOT)
        TEST_ROOT.mkdir(parents=True)

    def test_single_file_preserves_schema_under_hive_partition_directories(self) -> None:
        path = TEST_ROOT / "vintage=receipt" / "trade_date=2026-08-18" / "quotes.parquet"
        path.parent.mkdir(parents=True)
        # Exercise both legacy string and pandas 3 large_string artifacts.
        for dtype in (pa.string(), pa.large_string()):
            with self.subTest(dtype=dtype):
                table = pa.table({
                    "trade_date": pa.array(["2026-08-17", None], type=dtype),
                    "thscode": pa.array(["000001.SZ", "920001.BJ"], type=dtype),
                    "close": [10.0, 20.0],
                })
                pq.write_table(table, path)
                actual = read_parquet_file(path)
                pd.testing.assert_frame_equal(actual, table.to_pandas())
                self.assertNotIn("vintage", actual.columns)
                self.assertEqual(actual.loc[0, "trade_date"], "2026-08-17")
                pd.testing.assert_frame_equal(
                    read_parquet_file(path, columns=["thscode", "close"]),
                    table.select(["thscode", "close"]).to_pandas(),
                )

    def test_single_file_does_not_hide_missing_or_corrupt_artifacts(self) -> None:
        path = TEST_ROOT / "missing.parquet"
        with self.assertRaises(FileNotFoundError):
            read_parquet_file(path)
        path.write_bytes(b"not a parquet file")
        with self.assertRaises(pa.ArrowInvalid):
            read_parquet_file(path)

    def test_manifest_missing_is_missing_but_corruption_fails_closed(self) -> None:
        path = TEST_ROOT / "manifest.json"
        self.assertEqual(load_manifest(path), {})
        path.write_text("{}", encoding="utf-8")
        with self.assertRaisesRegex(ArtifactContractError, "empty object"):
            load_manifest(path)

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

    def test_oversize_research_fails_before_any_artifact_is_written(self):
        data_dir = TEST_ROOT / "data"
        snapshot = data_dir / "snapshots" / "2026-08-28"
        atomic_write_json(snapshot / "manifest.json", {"schema_version": 3})
        before = (snapshot / "manifest.json").read_bytes()
        with self.assertRaisesRegex(ArtifactContractError, "research hard limit"):
            publish_data_reference_artifacts(
                data_dir,
                "2026-08-28",
                pd.DataFrame(),
                {},
                {},
                {},
                {},
                research_artifacts={
                    "research_inputs_latest.json": {"text": "x" * (300 * 1024)}
                },
            )
        self.assertEqual(before, (snapshot / "manifest.json").read_bytes())
        self.assertFalse((snapshot / "stock_state.parquet").exists())


if __name__ == "__main__":
    unittest.main()
