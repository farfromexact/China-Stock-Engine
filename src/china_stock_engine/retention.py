"""Bound duplicate daily snapshots without deleting historical facts or factors."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path

from .storage import _prune_snapshots, atomic_write_json


def tree_bytes(root: Path) -> int:
    return sum(path.stat().st_size for path in root.rglob("*")
               if path.is_file() and not path.is_symlink()) if root.exists() else 0


def maintain(data_dir: Path, *, snapshot_limit: int = 5,
             max_data_bytes: int = 900_000_000) -> dict:
    before = tree_bytes(data_dir / "snapshots")
    _prune_snapshots(data_dir, snapshot_limit)
    after = tree_bytes(data_dir / "snapshots")
    data_bytes = tree_bytes(data_dir)
    status = {
        "schema_version": 1,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "state": "capacity_blocked" if data_bytes >= max_data_bytes else "capacity_ok",
        "data_bytes": data_bytes,
        "git_bytes": tree_bytes(data_dir.parent / ".git"),
        "max_data_bytes": max_data_bytes,
        "snapshot_limit": snapshot_limit,
        "snapshot_bytes_before": before,
        "snapshot_bytes_after": after,
        "facts_bytes": tree_bytes(data_dir / "facts"),
        "historical_facts_pruned": False,
    }
    # This small status can still be published when full-data publication is blocked.
    atomic_write_json(data_dir / "storage_status.json", status)
    return status


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    parser.add_argument("--snapshot-limit", type=int, default=5)
    parser.add_argument("--check-capacity", action="store_true")
    args = parser.parse_args()
    status = maintain(args.data_dir, snapshot_limit=args.snapshot_limit)
    print(status, flush=True)
    if status["data_bytes"] >= 700_000_000:
        print("::warning::Active data exceeds 700 MB; plan historical fact storage migration.")
    if args.check_capacity and status["state"] != "capacity_ok":
        print("::error::Active data exceeds 900 MB; storage_status.json records the publication block.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
