"""Immutable Parquet partitions with an atomic, local catalog."""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import tempfile

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

from .schema import (EVENT, INDUSTRY, MARKET, TRADABILITY, day_value, days_between, event_row,
                     industry_row, market_row, timestamp, tradability_row, utcnow)


def digest(file: Path) -> str:
    with file.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def json_digest(value) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def atomic_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, suffix=".tmp", mode="w",
                                     encoding="utf-8", delete=False) as handle:
        temporary = Path(handle.name)
        try:
            json.dump(value, handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        except BaseException:
            handle.close()
            temporary.unlink(missing_ok=True)
            raise
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def writer_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        if os.name == "nt":
            import msvcrt
            if handle.seek(0, 2) == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            acquire = lambda: msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            release = lambda: msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            acquire = lambda: fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            release = lambda: fcntl.flock(handle, fcntl.LOCK_UN)
        try:
            acquire()
        except OSError as exc:
            raise ValueError("数据仓已有写入进程") from exc
        try:
            yield
        finally:
            release()


def eligible(versions, cutoff: str):
    return next((row for row in reversed(versions) if row["known_at"] <= cutoff), None)


def validate_order(versions, known_at: str):
    if versions and versions[-1]["known_at"] >= known_at:
        raise ValueError("较旧或同一时刻的观察不能替代已存版本")


def unique_rows(rows, normalize, identity, *, empty=False):
    records = sorted((normalize(row) for row in rows), key=identity)
    if not records and not empty:
        raise ValueError("数据为空")
    if len({identity(row) for row in records}) != len(records):
        raise ValueError("数据有重复主键")
    return records


class Warehouse:
    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self.catalog = self.root / "catalog.json"

    def read(self) -> dict:
        if not self.catalog.exists():
            return {"schema": 1, "calendar": {}, "market": {}, "industry": {}, "events": {}, "tradability": {}}
        state = json.loads(self.catalog.read_text(encoding="utf-8"))
        state.setdefault("tradability", {})
        if state.get("schema") != 1 or any(not isinstance(state.get(key), dict)
                                            for key in ("calendar", "market", "industry", "events", "tradability")):
            raise ValueError("本地 catalog 结构无效")
        return state

    @contextmanager
    def edit(self):
        with writer_lock(self.root / ".writer.lock"):
            state = self.read()
            before = json_digest(state)
            yield state
            if json_digest(state) != before:
                atomic_json(self.catalog, state)

    def path(self, entry) -> Path:
        target = (self.root / entry["path"]).resolve()
        if not target.is_relative_to(self.root) or target.suffix != ".parquet":
            raise ValueError("catalog 中的数据路径无效")
        return target

    def _stage(self, folder: str, rows: list, schema: pa.Schema, semantic: str) -> dict:
        target = self.root / folder / f"{semantic}.parquet"
        if not target.resolve().is_relative_to(self.root):
            raise ValueError("数据路径必须在本仓内")
        target.parent.mkdir(parents=True, exist_ok=True)
        table = pa.Table.from_pylist(rows, schema=schema)
        if target.exists():
            # A previous process may have staged this file before being interrupted.
            if not pq.ParquetFile(target).read().equals(table):
                raise ValueError(f"同名 Parquet 内容不一致: {target.name}")
        else:
            with tempfile.NamedTemporaryFile(dir=target.parent, suffix=".tmp", delete=False) as handle:
                temporary = Path(handle.name)
            try:
                pq.write_table(table, temporary, compression="zstd")
                file_to_sync = os.open(temporary, os.O_RDWR)
                try:
                    os.fsync(file_to_sync)
                finally:
                    os.close(file_to_sync)
                os.replace(temporary, target)
            finally:
                temporary.unlink(missing_ok=True)
        return {"path": target.relative_to(self.root).as_posix(), "sha256": digest(target),
                "content": semantic, "rows": len(rows)}

    def add_calendar(self, state, start, end, open_days, known_at, *, source="Wind SSE"):
        known_at = timestamp(known_at)
        window = list(days_between(start, end))
        normalized = [day_value(day) for day in open_days]
        days = set(normalized)
        if len(days) != len(normalized) or not days <= set(window):
            raise ValueError("交易日历重复或超出已核验范围")
        changes = 0
        for day in window:
            versions = state["calendar"].setdefault(day, [])
            flag = day in days
            if versions and versions[-1]["open"] == flag:
                continue
            validate_order(versions, known_at)
            versions.append({"open": flag, "known_at": known_at, "source": source})
            changes += 1
        return changes

    def add_market(self, state, day, rows, known_at, *, source, universe,
                   min_securities=5000, min_coverage=0.98):
        day, known_at = day_value(day), timestamp(known_at)
        calendar = eligible(state["calendar"].get(day, []), known_at)
        if not calendar or not calendar["open"]:
            raise ValueError(f"{day} 不是已确认交易日")
        if universe not in {"daily", "legacy_window"}:
            raise ValueError("无效的股票池证明类型")
        records = unique_rows(rows, market_row, lambda row: row["code"])
        codes = {row["code"] for row in records}
        if len(codes) < min_securities or {code[-2:] for code in codes} != {"SH", "SZ", "BJ"}:
            raise ValueError(f"{day} 非完整沪深北股票池（{len(codes)} 只）")
        quoted = sum(row["state"] != "unobserved" for row in records)
        coverage = quoted / len(records)
        if coverage < min_coverage:
            raise ValueError(f"{day} 行情观察覆盖 {coverage:.2%} 低于门槛 {min_coverage:.2%}")
        versions = state["market"].setdefault(day, [])
        semantic = json_digest({"rows": records, "universe": universe, "source": source})
        if versions and versions[-1]["content"] == semantic:
            self._check_object(versions[-1], MARKET)
            return False
        validate_order(versions, known_at)
        artifact = self._stage(f"market/{day}", records, MARKET, semantic)
        versions.append({**artifact, "known_at": known_at, "source": source, "universe": universe,
                         "quoted": quoted, "coverage": coverage})
        return True

    def add_industry(self, rows, as_of, source, scheme, known_at=None) -> bool:
        as_of, known_at = day_value(as_of), timestamp(known_at or utcnow())
        records = unique_rows(rows, industry_row, lambda row: row["code"])
        if not source.strip() or not scheme.strip():
            raise ValueError("行业来源和口径不能为空")
        semantic = json_digest({"date": as_of, "rows": records, "source": source, "scheme": scheme})
        with self.edit() as state:
            versions = state["industry"].setdefault(scheme, [])
            existing = [entry for entry in versions if entry["as_of"] == as_of and entry["source"] == source]
            if existing and existing[-1]["content"] == semantic:
                self._check_object(existing[-1], INDUSTRY)
                return False
            validate_order(existing, known_at)
            artifact = self._stage(f"industry/{as_of}", records, INDUSTRY, semantic)
            versions.append({**artifact, "known_at": known_at, "source": source, "as_of": as_of})
            versions.sort(key=lambda entry: entry["known_at"])
        return True

    def add_tradability(self, rows, day, source, known_at=None):
        day, known_at = day_value(day), timestamp(known_at or utcnow())
        if not source.strip():
            raise ValueError("交易状态来源不能为空")
        records = unique_rows(rows, tradability_row, lambda row: row["code"])
        semantic = json_digest({"date": day, "source": source, "rows": records})
        with self.edit() as state:
            versions = state["tradability"].setdefault(day, [])
            if versions and versions[-1]["content"] == semantic:
                self._check_object(versions[-1], TRADABILITY)
                return False
            validate_order(versions, known_at)
            artifact = self._stage(f"tradability/{day}", records, TRADABILITY, semantic)
            versions.append({**artifact, "known_at": known_at, "source": source})
        return True

    def add_events(self, rows, source, known_at=None):
        if not source.strip():
            raise ValueError("公告来源不能为空")
        known_at = timestamp(known_at or utcnow())
        records = unique_rows(rows, event_row, lambda row: (row["event_id"], row["code"]), empty=True)
        source_folder = json_digest(source)[:16]
        with self.edit() as state:
            batches = state["events"].setdefault(source, [])
            indexed = self._matching_events(batches, records)
            changed = [row for row in records if indexed.get((row["event_id"], row["code"])) != row]
            if not changed:
                # No payload or artificial freshness marker for an empty or unchanged export.
                if not batches:
                    state["events"].pop(source)
                return 0
            validate_order(batches, known_at)
            semantic = json_digest({"source": source, "known_at": known_at, "rows": changed})
            artifact = self._stage(f"events/{source_folder}", changed, EVENT, semantic)
            batches.append({**artifact, "known_at": known_at, "source": source})
            return len(changed)

    def _matching_events(self, batches, rows):
        if not batches or not rows:
            return {}
        paths = [self.path(batch).as_posix() for batch in batches]
        with self.sql() as connection:
            connection.register("batch_order", pa.table({"path": paths, "version": list(range(len(paths)))}))
            connection.register("event_keys", pa.table({"event_id": [row["event_id"] for row in rows],
                                                        "code": [row["code"] for row in rows]}))
            connection.read_parquet(paths, filename=True).create_view("archive")
            result = connection.execute("""
                SELECT a.* EXCLUDE (filename)
                FROM archive a JOIN batch_order b ON replace(a.filename, chr(92), '/') = b.path
                JOIN event_keys k ON a.event_id = k.event_id AND a.code = k.code
                QUALIFY row_number() OVER (PARTITION BY a.event_id, a.code ORDER BY b.version DESC) = 1
            """)
            names = [column[0] for column in result.description]
            return {(row[0], row[1]): dict(zip(names, row)) for row in result.fetchall()}

    def _check_object(self, entry, schema):
        path = self.path(entry)
        if digest(path) != entry["sha256"]:
            raise ValueError(f"数据 SHA256 校验失败: {entry['path']}")
        file = pq.ParquetFile(path)
        if file.metadata.num_rows != entry["rows"] or not file.schema_arrow.equals(schema):
            raise ValueError(f"数据结构或行数异常: {entry['path']}")

    def validate(self):
        state = self.read()
        counts = {}
        for key, schema in (("market", MARKET), ("industry", INDUSTRY), ("events", EVENT),
                            ("tradability", TRADABILITY)):
            entries = [entry for versions in state[key].values() for entry in versions]
            for entry in {entry["path"]: entry for entry in entries}.values():
                self._check_object(entry, schema)
            counts[key] = len(entries)
        counts["calendar"] = len(state["calendar"])
        return counts

    def sql(self):
        return duckdb.connect(config={"threads": 4, "memory_limit": "1GB",
                                      "autoload_known_extensions": False, "autoinstall_known_extensions": False})
