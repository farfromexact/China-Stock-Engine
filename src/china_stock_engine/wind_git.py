"""Commit only verified generated Wind data, retain and retry failed pushes."""
from __future__ import annotations

import os
import json
from pathlib import Path
import shutil
import subprocess
import sys
import uuid

from .pipeline import validate_latest
from .retention import tree_bytes
from .storage import atomic_write_json, json_sha256, load_json_object, load_manifest, sha256_file
from .wind_session import collector_lock

ORIGINS = ("https://github.com/farfromexact/China-Stock-Engine.git",
           "https://github.com/farfromexact/China-Stock-Engine",
           "git@github.com:farfromexact/China-Stock-Engine.git")


class GitPublishError(RuntimeError):
    pass


def git_binary():
    candidates = [os.environ.get("CHINA_STOCK_GIT"), shutil.which("git")]
    if sys.platform == "darwin":
        candidates += ["/Library/Developer/CommandLineTools/usr/bin/git",
            str(Path.home() / ".cache/codex-runtimes/codex-primary-runtime/dependencies/bin/fallback/git")]
    for candidate in candidates:
        if not candidate:
            continue
        try:
            if subprocess.run([candidate, "--version"], capture_output=True, timeout=10).returncode == 0:
                return candidate
        except (OSError, subprocess.TimeoutExpired):
            pass
    raise GitPublishError("Git is unavailable; configure CHINA_STOCK_GIT or install Git")


class DataPublisher:
    def __init__(self, root: Path, workspace: Path, *, allowed_origins=ORIGINS):
        self.root, self.workspace = root.resolve(), workspace.resolve()
        self.git = git_binary()
        self.allowed_origins = allowed_origins
        self.journal = self.workspace / "git_pending.json"

    def run(self, *args):
        try:
            result = subprocess.run([self.git, "-C", str(self.root), *args], capture_output=True,
                text=True, encoding="utf-8", timeout=120, env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise GitPublishError(f"git {args[0]} unavailable/timed out; local data retained") from exc
        if result.returncode:
            # Git stderr/URLs can include credentials; do not echo either.
            raise GitPublishError(f"git {args[0]} failed (exit {result.returncode}); local data/commit retained")
        return result.stdout.rstrip("\r\n")

    def changed(self):
        # -z keeps spaces/unicode literal. Disable rename detection so each
        # retired/generated path is checked independently.
        output = self.run("status", "--porcelain=v1", "-z", "--untracked-files=all", "--no-renames")
        return [(record[:2], record[3:]) for record in output.split("\0") if record]

    def data_digest(self):
        if any(p.is_symlink() for p in (self.root / "data").rglob("*")):
            raise GitPublishError("Automatic publication refuses data symlinks")
        return json_sha256({p.relative_to(self.root / "data").as_posix(): sha256_file(p)
                            for p in sorted((self.root / "data").rglob("*")) if p.is_file()})

    def check_repository(self):
        if Path(self.run("rev-parse", "--show-toplevel")).resolve() != self.root:
            raise GitPublishError("Publication must use the repository root")
        if self.run("branch", "--show-current") != "main":
            raise GitPublishError("Automatic data publication requires main; do not switch a live collector")
        if self.run("remote", "get-url", "origin") not in self.allowed_origins:
            raise GitPublishError("origin is not the configured China-Stock-Engine remote")
        if any(url not in self.allowed_origins for url in self.run("remote", "get-url", "--push", "--all", "origin").splitlines()):
            raise GitPublishError("Push destination differs from the configured repository")

    def validate_data(self):
        valid, _ = validate_latest(data_dir=self.root / "data")
        manifest = load_manifest(self.root / "data/latest/manifest.json")
        receipt = load_json_object(self.root / "data/wind_publication_status.json", missing_ok=False)
        if (not valid or manifest.get("provider") != "windpy" or receipt.get("state") != "published"
                or manifest.get("trade_date") != receipt.get("trade_date")
                or manifest.get("wind_vintage") != receipt.get("vintage")):
            raise GitPublishError("Local Wind publication failed final validation; nothing committed")
        if tree_bytes(self.root / "data") >= 900_000_000:
            raise GitPublishError("Data exceeds the 900 MB publication gate")
        return manifest, receipt

    def allowed_paths(self, changes, manifest, receipt):
        latest = {"data/latest/" + name for name in manifest["artifacts"]}
        latest |= {"data/latest/manifest.json", "data/latest/last_attempt_status.json"}
        old_latest = set()
        if self.run("ls-tree", "HEAD", "--", "data/latest/manifest.json"):
            old = json.loads(self.run("show", "HEAD:data/latest/manifest.json"))
            old_latest = {"data/latest/" + name for name in old.get("artifacts", {})}
        roots = {"data/wind_publication_status.json", "data/last_run_status.json", "data/storage_status.json"}
        prefix = f"data/facts/wind/vintage={manifest['wind_vintage']}/"
        fact_names = {"daily_quotes.parquet", "daily_security_status.parquet", "market_receipt.json",
                      "adjustment_factors.parquet", "universe.parquet", "security_reference.parquet",
                      "trading_calendar.parquet", "market_summary.json", "manifest.json",
                      "wind_coverage.json", "candidate_receipt.json"}
        # Publication also creates a new technical research view outside latest.
        key = json_sha256({"pit_timing": manifest.get("feature_pit_timing") or {},
                          "feature_input_sha256": manifest.get("feature_input_sha256")})[:16]
        view = f"data/features/technical_signals/trade_date={manifest['trade_date']}/research_view={key}/"
        view_names = {"technical_scan.parquet", "technical_signals_latest.json", "signal_receipt.json"}
        if any(path.startswith(view) for _, path in changes):
            signal = load_json_object(self.root / view / "signal_receipt.json", missing_ok=False)
            if signal.get("feature_input_sha256") != manifest.get("feature_input_sha256") or signal.get("trade_date") != manifest["trade_date"]:
                raise GitPublishError("Technical research view is not bound to latest")
            for name in view_names - {"signal_receipt.json"}:
                expected = manifest["artifacts"].get(name, {}).get("sha256")
                if (not expected or signal.get("artifacts", {}).get(name, {}).get("sha256") != expected
                        or sha256_file(self.root / view / name) != expected):
                    raise GitPublishError("Technical research view hash does not match latest")
        retired = ["data/" + name + "/" for name in receipt.get("retired_local_daily_vintages", [])]
        for state, path in changes:
            generated = (path in latest or path in roots
                         or (path.startswith(prefix) and Path(path).name in fact_names)
                         or (path.startswith(view) and Path(path).name in view_names))
            retired_file = state.strip() == "D" and any(path.startswith(p) for p in retired)
            retired_file = retired_file or (state.strip() == "D" and path in old_latest)
            if not path.startswith("data/") or not (generated or retired_file):
                raise GitPublishError("Unrelated edits are present; no automatic commit was made")

    def sync(self):
        self.check_repository()
        changes = self.changed()
        info = load_json_object(self.journal)
        if any(not path.startswith("data/") for _, path in changes):
            raise GitPublishError("Non-data edits are present; finish them before automatic publication")
        if not info and not changes:
            if self.run("rev-parse", "HEAD") != self.run("rev-parse", "refs/remotes/origin/main"):
                raise GitPublishError("Local main has unsynchronized history; no unrelated commits will be pushed")
            return {"state": "no_changes", "pushed": False}
        manifest, receipt = self.validate_data()
        if not info:
            if any(state[0] != " " and state != "??" for state, _ in changes):
                raise GitPublishError("User-staged changes are present; index left untouched")
            self.allowed_paths(changes, manifest, receipt)
            base = self.run("rev-parse", "HEAD")
            self.run("fetch", "origin", "main")
            if self.run("rev-parse", "FETCH_HEAD") != base:
                raise GitPublishError("Remote/local main differ; local data retained for explicit synchronization")
            info = {"schema_version": 1, "root": str(self.root), "base": base,
                    "paths": sorted(path for _, path in changes), "digest": self.data_digest(),
                    "message": f"data: Wind {receipt['trade_date']} ({uuid.uuid4().hex[:12]})",
                    "trade_date": receipt["trade_date"]}
            atomic_write_json(self.journal, info)
        if info["root"] != str(self.root) or self.data_digest() != info["digest"]:
            raise GitPublishError("Pending publication data changed; inspect it before retry")
        head = self.run("rev-parse", "HEAD")
        if head == info["base"]:
            if sorted(path for _, path in changes) != info["paths"]:
                raise GitPublishError("Pending change set changed; nothing committed")
            self.workspace.mkdir(parents=True, exist_ok=True)
            pathspec = self.workspace / "git-paths.nul"
            pathspec.write_bytes(b"\0".join(path.encode("utf-8") for path in info["paths"]) + b"\0")
            try:
                self.run("add", "-A", "--pathspec-from-file=" + str(pathspec), "--pathspec-file-nul")
                # --only commits precisely these paths and preserves other index
                # entries. A NUL pathspec also works within Windows command limits.
                self.run("commit", "--only", "-m", info["message"],
                         "--pathspec-from-file=" + str(pathspec), "--pathspec-file-nul")
            finally:
                pathspec.unlink(missing_ok=True)
            head = self.run("rev-parse", "HEAD")
        if (self.run("rev-parse", head + "^") != info["base"]
                or self.run("log", "-1", "--format=%s") != info["message"]
                or sorted(self.run("diff", "--name-only", "--no-renames", info["base"], head).splitlines()) != info["paths"]):
            raise GitPublishError("Pending commit history differs; no push attempted")
        info["commit"] = head
        atomic_write_json(self.journal, info)
        if self.changed() or self.data_digest() != info["digest"]:
            raise GitPublishError("Checkout changed after commit; commit retained, push deferred")
        self.run("fetch", "origin", "main")
        remote = self.run("rev-parse", "FETCH_HEAD")
        if remote == info["base"]:
            self.run("push", "origin", head + ":refs/heads/main")
        elif remote != head:
            # Do not push unrelated commits, rebase generated data or force-push.
            try:
                self.run("merge-base", "--is-ancestor", head, remote)
            except GitPublishError as exc:
                raise GitPublishError("Remote advanced independently; pending commit retained for review") from exc
        self.journal.unlink()
        return {"state": "pushed", "pushed": True, "commit": head, "trade_date": info["trade_date"]}


def sync_data(root: Path, data_dir: Path, workspace: Path):
    if data_dir.resolve() != root.resolve() / "data":
        raise GitPublishError("Automatic Git publication requires the repository data/ directory")
    with collector_lock(root / ".private-stock-data/wind-publication.lock"):
        return DataPublisher(root, workspace).sync()
