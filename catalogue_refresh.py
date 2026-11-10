"""Scheduled, single-flight refresh for Literary Roulette's local Gutenberg catalogue.

The web process serves the last verified snapshot immediately. A daemon thread
checks periodically and refreshes only when the snapshot is stale. An OS file
lock prevents duplicate archive downloads across Flask/Gunicorn processes.
"""
from __future__ import annotations

import fcntl
import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_status(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _snapshot_age_seconds(snapshot_path: Path) -> float | None:
    try:
        return max(0.0, datetime.now(timezone.utc).timestamp() - snapshot_path.stat().st_mtime)
    except OSError:
        return None




def _snapshot_is_current(snapshot_path: Path) -> bool:
    """Only a complete snapshot from the new RDF importer counts as current.

    This deliberately forces migration from an old Gutendex page preview/full
    API snapshot even when that JSON file was written less than 24 hours ago.
    """
    data = _read_json(snapshot_path)
    if data.get("sync_complete") is not True:
        return False
    if data.get("provider") != "Project Gutenberg official RDF metadata archive":
        return False
    languages = data.get("languages")
    counts = data.get("counts")
    if not isinstance(languages, dict) or not isinstance(counts, dict):
        return False
    return all(
        isinstance(languages.get(code), list)
        and len(languages[code]) > 0
        and counts.get(code) == len(languages[code])
        for code in ("en", "fr", "zh")
    )


def _age_of_iso(value) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return max(0.0, (datetime.now(timezone.utc) - parsed).total_seconds())
    except ValueError:
        return None


def start_catalogue_refresh(
    *,
    project_root: Path,
    snapshot_path: Path,
    archive_path: Path,
    status_path: Path,
    lock_path: Path,
    after_success: Callable[[], None] | None = None,
    logger=None,
    max_age_hours: float = 24,
    retry_after_seconds: int = 3600,
    check_interval_seconds: int = 900,
    enabled: bool = True,
) -> threading.Thread | None:
    """Keep one daemon refresh loop alive; never block Flask startup or page requests."""
    if not enabled:
        return None

    script_path = project_root / "tools" / "sync_gutenberg_rdf.py"
    if not script_path.is_file():
        if logger:
            logger.warning("Gutenberg RDF sync script is missing: %s", script_path)
        return None

    period = max(60.0, float(max_age_hours) * 3600.0)
    retry_delay = max(60, int(retry_after_seconds))
    check_every = max(30, int(check_interval_seconds))

    def worker() -> None:
        while True:
            age = _snapshot_age_seconds(snapshot_path)
            previous = _read_json(status_path)
            orphaned_run = previous.get("state") == "running"
            stale = (not _snapshot_is_current(snapshot_path)) or age is None or age >= period or orphaned_run

            # Do not hammer the source if the previous update failed. All app
            # workers read the same status file and therefore share this cooldown.
            failed_age = _age_of_iso(previous.get("finished_at")) if previous.get("state") == "failed" else None
            in_failure_cooldown = failed_age is not None and failed_age < retry_delay

            if not stale or in_failure_cooldown:
                if age is None:
                    wait = check_every
                elif in_failure_cooldown:
                    wait = min(check_every, max(30, retry_delay - failed_age))
                else:
                    wait = min(check_every, max(30, period - age))
                time.sleep(wait)
                continue

            lock_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                with lock_path.open("a+") as lock_file:
                    try:
                        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        time.sleep(30)
                        continue

                    # Recheck after acquiring the lock: another worker may have
                    # completed the refresh while this worker was waiting.
                    age = _snapshot_age_seconds(snapshot_path)
                    previous = _read_json(status_path)
                    orphaned_run = previous.get("state") == "running"
                    if _snapshot_is_current(snapshot_path) and age is not None and age < period and not orphaned_run:
                        continue

                    state = {
                        "state": "running",
                        "started_at": _now(),
                        "finished_at": None,
                        "last_success_at": previous.get("last_success_at"),
                        "last_error": None,
                        "snapshot_path": str(snapshot_path),
                    }
                    _write_status(status_path, state)
                    if logger:
                        logger.info("Starting background Project Gutenberg catalogue refresh.")

                    command = [
                        sys.executable,
                        str(script_path),
                        "--archive", str(archive_path),
                        "--output", str(snapshot_path),
                        "--timeout", "180",
                        "--retries", "4",
                    ]
                    try:
                        result = subprocess.run(
                            command,
                            cwd=str(project_root),
                            text=True,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT,
                            check=False,
                        )
                        if result.returncode != 0:
                            tail = (result.stdout or "Catalogue update did not complete.")[-1800:]
                            raise RuntimeError(tail.strip())
                        if after_success:
                            after_success()
                        completed = {
                            **state,
                            "state": "success",
                            "finished_at": _now(),
                            "last_success_at": _now(),
                            "last_error": None,
                            "log_tail": (result.stdout or "").splitlines()[-12:],
                        }
                        _write_status(status_path, completed)
                        if logger:
                            logger.info("Background Gutenberg catalogue refresh completed.")
                    except Exception as exc:
                        failed = {
                            **state,
                            "state": "failed",
                            "finished_at": _now(),
                            "last_success_at": previous.get("last_success_at"),
                            "last_error": str(exc)[-1800:],
                        }
                        _write_status(status_path, failed)
                        if logger:
                            logger.exception("Background Gutenberg catalogue refresh failed; the previous snapshot remains in use.")
                    finally:
                        try:
                            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
                        except OSError:
                            pass
            except OSError as exc:
                if logger:
                    logger.warning("Could not open the Gutenberg catalogue refresh lock: %s", exc)
                time.sleep(60)
                continue

            # Check again soon. A successful atomic catalogue update makes the
            # snapshot fresh; a failed one is protected by the shared cooldown.
            time.sleep(30)

    thread = threading.Thread(target=worker, name="literary-roulette-catalogue-refresh", daemon=True)
    thread.start()
    return thread


def reload_pool_if_changed(pool, catalogues: dict, *, force: bool = False) -> bool:
    """Reload a GutendexPool in-place when another worker publishes a new snapshot."""
    path = Path(pool.catalog_path)
    try:
        mtime_ns = path.stat().st_mtime_ns
    except OSError:
        return False
    if not force and getattr(pool, "_lr_loaded_mtime_ns", None) == mtime_ns:
        return False

    reload_lock = getattr(pool, "_lr_reload_lock", None)
    if reload_lock is None:
        reload_lock = threading.Lock()
        pool._lr_reload_lock = reload_lock
    with reload_lock:
        try:
            mtime_ns = path.stat().st_mtime_ns
        except OSError:
            return False
        if not force and getattr(pool, "_lr_loaded_mtime_ns", None) == mtime_ns:
            return False
        from gutenberg_pool import GutendexPool
        fresh = GutendexPool(path, pool.cache_dir)
        # Keep the existing text-fetch lock; API requests may be reading cached text.
        state = dict(fresh.__dict__)
        state["_lock"] = getattr(pool, "_lock", threading.Lock())
        state["_lr_reload_lock"] = reload_lock
        state["_lr_loaded_mtime_ns"] = mtime_ns
        pool.__dict__.update(state)
        pool.attach_popularity(catalogues)
        pool.prewarm(catalogues)
        return True


def catalogue_status(status_path: Path, snapshot_path: Path, pool) -> dict:
    state = _read_json(status_path)
    age = _snapshot_age_seconds(snapshot_path)
    has_snapshot = age is not None
    state_name = state.get("state") or ("ready" if has_snapshot else "missing")
    counts = {code: pool.count(code) for code in ("en", "fr", "zh")}
    return {
        "ok": True,
        "state": state_name,
        "running": state_name == "running",
        "has_snapshot": has_snapshot,
        "snapshot_age_seconds": int(age) if age is not None else None,
        "last_success_at": state.get("last_success_at"),
        "started_at": state.get("started_at"),
        "finished_at": state.get("finished_at"),
        "last_error": state.get("last_error"),
        "counts": counts,
        "partial_snapshot": bool(getattr(pool, "is_partial_snapshot", False)),
    }
