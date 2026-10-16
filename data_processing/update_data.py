"""Validated data refresh, isolated build, archived evidence and atomic file publication.

python data_processing/update_data.py --sources nbs,cih --force
python data_processing/update_data.py --offline  # replay + validate without crawling
GitHub Actions and the optional local launcher use the same isolated updater.
"""

import argparse
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from data_store import ROOT, atomic_json, archive_project
import update_workspace as workspace
from update_workspace import (
    UpdateWorkspace,
    atomic_copy,
)
from update_sources import SourceTasks, DASHBOARD_SOURCES, now, read, due

BASE = ROOT / "data_processing"
FRONT = BASE / "frontend"
STATUS = FRONT / "update_status.json"


def single_writer():
    return UpdateWorkspace(ROOT, BASE).single_writer()


def copy_artifact(src, dst):
    return workspace.copy_artifact(src, dst, copy_file=atomic_copy)


def prepare_workspace(stage, dashboard_only=False):
    return UpdateWorkspace(ROOT, BASE).prepare_workspace(stage, dashboard_only)


def preserve_evidence(stage):
    return UpdateWorkspace(ROOT, BASE).preserve_evidence(stage)


def preserve_fang_state(stage):
    return UpdateWorkspace(ROOT, BASE).preserve_fang_state(stage)


def cleanup_workspace(stage):
    return UpdateWorkspace(ROOT, BASE).cleanup_workspace(stage)


def publish(stage, relative_paths):
    return UpdateWorkspace(ROOT, BASE).publish(stage, relative_paths, copy_artifact)


def source_job(name, options, status):
    return SourceTasks(ROOT, run_script).run(name, options)


def run_script(name, *args):
    result = subprocess.run(
        [sys.executable, str(BASE / name), *args], cwd=ROOT, timeout=1200
    )
    if result.returncode:
        raise RuntimeError(name + " 校验/构建失败")


def worker(names, offline=False, dashboard_only=False):
    previous = read(FRONT / "intersection_housing_data.json")
    if dashboard_only:
        from release_inputs import restore

        restore()
    policy = read(BASE / "update_policy.json")
    status = read(STATUS, {"sources": {}})
    status.setdefault("sources", {})
    successes = []
    for name in names:
        entry = dict(
            status["sources"].get(name, {}), checked_at=now(), state="checking"
        )
        try:
            info = source_job(name, policy["sources"][name], status)
            if info.get("skipped"):
                entry.update(
                    info,
                    state="cooldown" if info.get("state") == "cooldown" else "not_due",
                    error=None,
                )
            else:
                entry.update(
                    info,
                    state="partial" if info.get("partial") else "ok",
                    last_success=info.get("last_success") or now(),
                    error=info.get("error"),
                )
                successes.append(name)
        except Exception as exc:
            entry.update(state="failed", error=str(exc))
        status["sources"][name] = entry
        atomic_json(STATUS, status)
        print(name + ": " + entry["state"], flush=True)
    if not successes and not offline:
        skipped = bool(names) and all(
            status["sources"][n]["state"] in ("cooldown", "not_due") for n in names
        )
        status.update(
            state="retained",
            checked_at=now(),
            detail="来源冷却或尚未到期，本次未发送请求；已发布数据保留"
            if skipped
            else "本次采集未成功，继续使用已发布数据",
        )
        atomic_json(STATUS, status)
        return 3 if skipped else 2
    if offline or any(s in successes for s in DASHBOARD_SOURCES):
        from validate_index_sources import validate

        results = validate()
        if not results or any(r["status"] != "passed" for r in results):
            raise RuntimeError("统计局来源交叉校验失败，禁止发布")
        run_script("audit_price_publication.py", "--capture")
        run_script("engine_policy.py")
        # Do not run obsolete fixed-reference metadata calibration or listing normalization.
        run_script("city_intersection_filter.py")
        run_script("inverse_price_engine.py")
        for script in (
            "data_quality.py",
            "housing_segments.py",
            "information_hub.py",
            "audit_price_publication.py",
        ):
            run_script(script)
    if dashboard_only:
        from release_contract import validate
        from release_inputs import capture

        checks = validate(read(FRONT / "intersection_housing_data.json"), previous)
        capture()
        status["sources"] = {
            key: value
            for key, value in status["sources"].items()
            if key in DASHBOARD_SOURCES
        }
        status.update(
            state="ready",
            checked_at=now(),
            last_publish=now(),
            error=None,
            detail="主看板数据口径与历史保留校验通过",
            validation=checks,
        )
        atomic_json(STATUS, status)
        run_script("build_standalone.py")
        run_script("verify_frontend_data.py", "--dashboard-only")
        return 0
    if (
        "income" in successes
        and not offline
        and not any(s in successes for s in ("nbs", "anjuke", "cih"))
    ):
        run_script("information_hub.py")
    if (ROOT / "output/fang/fang.sqlite3").exists():
        from fang_audit import audit as audit_fang

        if audit_fang()["state"] != "passed":
            raise RuntimeError("房天下证据或字段校验失败，禁止发布")
    from community_crawler import build

    communities = build()
    from hydrology_crawler import build as build_water

    water = build_water()
    # Standalone collector commands also leave provenance-rich operational logs.
    # Reconcile those after validation, without inventing a new source check time.
    for name, dataset in [("communities", communities), ("hydrology", water)]:
        entries = list(read(ROOT / "output" / name / "status.json").values())
        latest = max(entries, key=lambda e: e.get("checked_at", ""), default={})
        prior = status["sources"].get(name, {})
        if latest.get("checked_at") and latest["checked_at"] >= prior.get(
            "checked_at", ""
        ):
            cities = dataset.get("cities", {})
            count = sum(c["count"] for c in cities.values())
            status["sources"][name] = dict(
                checked_at=latest["checked_at"],
                last_success=max(
                    (e.get("last_success", "") for e in entries), default=""
                )
                or None,
                state="ok" if latest.get("status") == "ok" else "partial",
                detail=f"已收录 {len(cities)} 城、{count:,} 处"
                + (
                    "小区 / 公寓位置"
                    if name == "communities"
                    else "江河及大型水体要素（排除小溪沟渠）"
                )
                + "；其他城市分批补充",
                error=latest.get("error"),
            )
    profiles = read(ROOT / "output/fang/status.json")
    if profiles:
        prior = status["sources"].get("community_profiles", {})
        last = profiles.get("last_run") or {}
        pause = profiles.get("pause") or {}
        status["sources"]["community_profiles"] = dict(
            prior,
            checked_at=last.get("checked_at") or prior.get("checked_at"),
            last_success=profiles.get("last_success"),
            state="cooldown"
            if pause.get("until", "") > now()
            else prior.get("state", "not_due"),
            detail=f"房天下 {profiles.get('profiles', 0)} 条资料、{profiles.get('with_price', 0)} 条有参考价；地图已匹配 {communities.get('fang_stats', {}).get('matched', 0)} 条",
            retry_after=pause.get("until"),
            error=pause.get("reason"),
        )
    coverage = list(
        read(ROOT / "output/communities/neighbourhood_status.json").values()
    )
    if coverage:
        latest = max(coverage, key=lambda e: e.get("checked_at", ""))
        status["sources"]["community_coverage"] = dict(
            checked_at=latest["checked_at"],
            state="ok"
            if all(e["status"] == "ok" for e in coverage)
            else "partial"
            if any(e["status"] == "ok" for e in coverage)
            else "failed",
            last_success=max((e.get("last_success", "") for e in coverage), default="")
            or None,
            detail="北京、上海遗漏住宅片区点位增量补查；失败保留原位置库",
            error=latest.get("error"),
        )
    if not (FRONT / "realestate/data/communities.json").exists():
        raise RuntimeError("小区位置库缺失")
    status.update(
        state="ready",
        checked_at=now(),
        last_publish=now(),
        detail="来源增量合并与发布校验通过；刷新页面读取新数据",
    )
    atomic_json(STATUS, status)
    run_script("build_standalone.py")
    run_script("frontend/realestate/build_inline.py")
    run_script("verify_frontend_data.py")
    return 0


def update(names=None, force=False, offline=False, dashboard_only=False):
    policy = read(BASE / "update_policy.json")
    status = read(STATUS, {"sources": {}})
    names = names or (
        list(DASHBOARD_SOURCES) if dashboard_only else list(policy["sources"])
    )
    if dashboard_only and any(n not in DASHBOARD_SOURCES for n in names):
        raise ValueError("Map source excluded from dashboard update")
    if any(n not in policy["sources"] for n in names):
        raise ValueError("Unknown source")
    selected = (
        []
        if offline
        else [
            n
            for n in names
            if force
            or due(status.get("sources", {}).get(n, {}), policy["sources"][n]["hours"])
        ]
    )
    if not selected and not offline:
        print("No sources due.")
        return 0
    with single_writer():
        UpdateWorkspace(ROOT, BASE).reap_stale_workspaces()
        archive_project()
        from snapshot_archive import SnapshotArchive
        from compact_storage import compact_clean
        with SnapshotArchive(ROOT) as history:
            compact_clean(history, ROOT)
        parent = ROOT / "output/update_work"
        parent.mkdir(parents=True, exist_ok=True)
        stage = Path(tempfile.mkdtemp(prefix="run-", dir=parent))
        atomic_json(stage / '.update-workspace.json', {'created_at': now()})
        logdir = ROOT / "output/update_logs"
        logdir.mkdir(exist_ok=True)
        logpath = logdir / (
            datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + ".log"
        )
        old_status = dict(status)
        status.update(
            state="checking",
            checked_at=now(),
            error=None,
            detail="后台采集与校验中，当前页面继续使用上次发布的数据",
        )
        atomic_json(STATUS, status)
        try:
            prepare_workspace(stage, dashboard_only)
            with logpath.open("w", encoding="utf-8") as out:
                args = [
                    sys.executable,
                    str(stage / "data_processing/update_data.py"),
                    "--worker",
                    "--sources",
                    ",".join(selected),
                ]
                if offline:
                    args.append("--offline")
                if dashboard_only:
                    args.append("--dashboard-only")
                result = subprocess.run(
                    args, cwd=stage, stdout=out, stderr=subprocess.STDOUT, timeout=2400
                )
            preserve_evidence(stage)
            candidate = read(
                stage / "data_processing/frontend/update_status.json", old_status
            )
            # Attempt cursors are operational state, not published housing observations.
            for rel in [
                "output/communities/status.json",
                "output/communities/supplement_status.json",
                "output/communities/neighbourhood_status.json",
                "output/community_profiles/status.json",
                "output/hydrology/status.json",
                "output/anjuke_recheck/update_rotation.json",
            ]:
                if (stage / rel).exists():
                    atomic_copy(stage / rel, ROOT / rel)
            if dashboard_only and result.returncode:
                from release_inputs import checkpoint_rotation

                checkpoint_rotation(ROOT)
            if result.returncode == 3:
                candidate["last_publish"] = old_status.get("last_publish")
                atomic_json(STATUS, candidate)
                print(
                    "Sources cooling down or not due; retained published data. Log: "
                    + str(logpath)
                )
                return 0
            if result.returncode:
                candidate.update(
                    state="retained",
                    last_publish=old_status.get("last_publish"),
                    detail="更新未通过或来源暂不可用，已保留上次发布数据；详情见更新日志",
                )
                atomic_json(STATUS, candidate)
                return result.returncode
            rels = UpdateWorkspace(ROOT, BASE).publication_paths(stage, dashboard_only)
            publish(stage, rels)
            archive_project()
            print("Published validated update. Log: " + str(logpath))
            return 0
        except Exception as exc:
            preserve_evidence(stage)
            cursor = Path("output/anjuke_recheck/update_rotation.json")
            if dashboard_only and (stage / cursor).is_file():
                try:
                    from release_inputs import checkpoint_rotation

                    atomic_copy(stage / cursor, ROOT / cursor)
                    checkpoint_rotation(ROOT)
                except Exception as cursor_exc:
                    exc = RuntimeError(
                        str(exc) + "; rotation checkpoint failed: " + str(cursor_exc)
                    )
            old_status.update(
                state="retained",
                checked_at=now(),
                detail="更新失败，保留已发布数据",
                error=str(exc),
            )
            atomic_json(STATUS, old_status)
            print(str(exc))
            return 1
        finally:
            cleanup_workspace(stage)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--sources")
    p.add_argument("--force", action="store_true")
    p.add_argument("--offline", action="store_true")
    p.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    p.add_argument(
        "--dashboard-only",
        action="store_true",
        help="只更新主看板；不采集、构建或校验小区地图",
    )
    a = p.parse_args()
    names = a.sources.split(",") if a.sources else []
    try:
        sys.exit(
            worker(names, a.offline, a.dashboard_only)
            if a.worker
            else update(names, a.force, a.offline, a.dashboard_only)
        )
    except (OSError, RuntimeError) as exc:
        print("Update not started: " + str(exc))
        sys.exit(1)
