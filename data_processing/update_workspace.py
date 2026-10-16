"""Isolated update workspaces, consistent backups and rollback publication."""

import contextlib
import os
import shutil
from pathlib import Path

from data_store import sqlite_snapshot

INPUT_DIRS = (
    "anjuke",
    "stats_gov",
    "income",
    "cih",
    "market",
    "anjuke_recheck",
    "engine_validation",
    "policies",
    "public_locations",
    "administrative_regions",
    "communities",
    "community_profiles",
    "fang",
    "hydrology",
    "housing_segments",
    "raw",
    "catalog",
    "community_baseline",
    "verification",
)

DASHBOARD_INPUT_DIRS = (
    "anjuke",
    "stats_gov",
    "income",
    "cih",
    "market",
    "anjuke_recheck",
    "engine_validation",
    "policies",
    "macro",
)

SQLITE_ARTIFACTS = ("output/catalog/catalog.sqlite3",)

PUBLISH_EXTRA = ("output/community_baseline/baseline_report.md",)

PRESERVE_DIRS = (
    "output/warehouse",
    "output/raw",
    "output/market/raw",
    "output/cih/raw",
    "output/catalog",
    "output/community_baseline",
    "output/verification",
)


def atomic_copy(src, dst):
    dst.parent.mkdir(parents=True, exist_ok=True)
    temp = dst.with_name(dst.name + ".publish.tmp")
    shutil.copy2(src, temp)
    os.replace(temp, dst)


def copy_artifact(src, dst, copy_file=atomic_copy):
    """Consistent copy: SQLite uses the backup API, everything else tmp+replace."""
    if Path(src).suffix in (".sqlite3", ".sqlite"):
        return sqlite_snapshot(src, dst)
    return copy_file(src, dst)


class UpdateWorkspace:
    """Own filesystem changes for one project without changing source observations."""

    def __init__(self, root, base=None):
        self.root = Path(root)
        self.base = Path(base) if base is not None else self.root / "data_processing"

    @contextlib.contextmanager
    def single_writer(self):
        """OS lock releases on a crash; no stale PID file can block future updates."""
        path = self.root / "output/update.lock"
        path.parent.mkdir(exist_ok=True)
        with path.open("a+b") as f:
            if f.tell() == 0:
                f.write(b"0")
                f.flush()
            f.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                yield
            finally:
                f.seek(0)
                if os.name == "nt":
                    msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(f, fcntl.LOCK_UN)

    def prepare_workspace(self, stage, dashboard_only=False):
        excluded = [
            "__pycache__",
            ".pytest_cache",
            ".mypy_cache",
            ".ruff_cache",
            "standalone_dashboard.html",
            "map_inline.html",
        ]
        if dashboard_only:
            excluded.append("realestate")
        shutil.copytree(
            self.base,
            stage / "data_processing",
            ignore=shutil.ignore_patterns(*excluded),
        )
        for name in DASHBOARD_INPUT_DIRS if dashboard_only else INPUT_DIRS:
            source = self.root / "output" / name
            if source.exists():
                shutil.copytree(source, stage / "output" / name)
        for rel in () if dashboard_only else SQLITE_ARTIFACTS:
            source = self.root / rel
            if source.exists():
                sqlite_snapshot(source, stage / rel)

        def immutable_copy(src, dst):
            try:
                os.link(src, dst)
            except OSError:
                shutil.copy2(src, dst)

        source = self.root / "output/warehouse"
        if source.exists():
            shutil.copytree(
                source, stage / "output/warehouse", copy_function=immutable_copy,
                ignore=shutil.ignore_patterns('history.sqlite3', 'history.sqlite3-journal', 'history.sqlite3-wal', 'history.sqlite3-shm')
            )

    def preserve_evidence(self, stage):
        from snapshot_archive import SnapshotArchive, ARCHIVE
        if (stage / ARCHIVE).is_file():
            with SnapshotArchive(self.root) as archive:
                archive.merge(stage)
        for relative in PRESERVE_DIRS:
            source = stage / relative
            for path in source.rglob("*"):
                if path.is_file() and path.suffix not in (".sqlite3", ".sqlite"):
                    target = self.root / relative / path.relative_to(source)
                    if not target.exists():
                        atomic_copy(path, target)
        for rel in SQLITE_ARTIFACTS:
            source = stage / rel
            if source.exists() and (not (self.root / rel).exists()):
                sqlite_snapshot(source, self.root / rel)
        self.preserve_fang_state(stage)

    def preserve_fang_state(self, stage):
        """Crawl results/cursors survive a failed frontend build. Backup a transactionally valid DB."""
        import sqlite3

        source = stage / "output/fang/fang.sqlite3"
        if not source.exists():
            return
        target = self.root / "output/fang/fang.sqlite3"
        target.parent.mkdir(parents=True, exist_ok=True)
        temp = target.with_suffix(".backup.tmp")
        with (
            contextlib.closing(sqlite3.connect(source)) as src,
            contextlib.closing(sqlite3.connect(temp)) as dst,
        ):
            src.backup(dst)
        os.replace(temp, target)
        from fang_store import Store

        store = Store(target.parent)
        try:
            store.export()
        finally:
            store.close()

    def cleanup_workspace(self, stage):
        resolved = stage.resolve()
        allowed = (self.root / "output/update_work").resolve()
        if resolved == allowed or allowed not in resolved.parents:
            raise ValueError("Refusing cleanup outside updater workspace")
        shutil.rmtree(resolved)

    def reap_stale_workspaces(self):
        """Caller holds single_writer; preserve abandoned worker diagnostics before removal."""
        import json
        import time
        from compact_storage import archive_tree, read_failure
        from snapshot_archive import SnapshotArchive
        parent = self.root / 'output/update_work'
        for stage in parent.glob('run-*'):
            try:
                marker = stage / '.update-workspace.json'
                if not marker.is_file() or time.time() - marker.stat().st_mtime < 24 * 3600:
                    continue
                if stage.resolve().parent != parent.resolve() or os.path.isjunction(stage):
                    raise ValueError('Invalid stale updater workspace')
                with SnapshotArchive(self.root) as archive:
                    archive_tree(archive, stage)
                self.cleanup_workspace(stage)
            except PermissionError as error:
                print(json.dumps(dict(event='stale-workspace-preserved',
                    **read_failure(error, self.root, 'reap-stale-workspace')),
                    ensure_ascii=False), flush=True)

    def publication_paths(self, stage, dashboard_only=False):
        """Select validated artifacts once, with update status published last."""
        paths = []
        for folder in ("anjuke", "stats_gov") if dashboard_only else INPUT_DIRS:
            paths.extend(
                p.relative_to(stage)
                for p in (stage / "output" / folder).rglob("*.json")
            )
        paths.extend(
            p.relative_to(stage)
            for p in (stage / "data_processing/frontend").glob("*.json")
        )
        paths.append(Path("data_processing/standalone_dashboard.html"))
        if dashboard_only:
            from release_inputs import INPUTS

            paths.extend(Path(rel) for rel in INPUTS)
            paths.extend(
                p.relative_to(stage)
                for p in (stage / "output/engine_validation").glob("20??-??.json")
            )
            paths.append(Path("data_processing/published_inputs.zip"))
        else:
            paths.extend(
                p.relative_to(stage)
                for p in (stage / "data_processing/frontend/realestate/data").rglob(
                    "*.json"
                )
            )
            paths.extend(
                Path(rel)
                for rel in (
                    "data_processing/frontend/realestate/map_inline.html",
                    "data_processing/frontend/realestate/assets/js/map-app.js",
                    *SQLITE_ARTIFACTS,
                    *PUBLISH_EXTRA,
                )
            )
        status = Path("data_processing/frontend/update_status.json")
        return list(dict.fromkeys(p for p in paths if p != status)) + [status]

    def publish(self, stage, relative_paths, copy_artifact):
        """Publish individual JSON/bundles atomically, rollback all touched files if one fails."""
        backup = stage / "rollback"
        touched = []
        try:
            for rel in relative_paths:
                src, dst = (stage / rel, self.root / rel)
                if not src.is_file():
                    continue
                existed = dst.exists()
                if existed:
                    old = backup / rel
                    old.parent.mkdir(parents=True, exist_ok=True)
                    copy_artifact(dst, old)
                copy_artifact(src, dst)
                touched.append((rel, existed))
        except Exception:
            for rel, existed in reversed(touched):
                if existed:
                    copy_artifact(backup / rel, self.root / rel)
                else:
                    (self.root / rel).unlink(missing_ok=True)
            raise
