#!/usr/bin/env python3
"""
Daily health check for @tatamispaces automation.

Checks: imports, JSON integrity, auth status, log sizes,
post queue, stale lockfiles, disk space.

Usage:
    python healthcheck.py          # Run checks, report
    python healthcheck.py --fix    # Auto-fix: archive old logs, remove stale locks
"""

import sys
import os
import json
import shutil
import argparse
from pathlib import Path
from datetime import datetime, timezone, timedelta

sys.path.insert(0, str(Path(__file__).parent))

from tools.common import load_json, notify, setup_logging, load_config

log = setup_logging("healthcheck")

BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"


def check_imports() -> tuple[bool, str]:
    """Try importing every script module to catch broken dependencies."""
    modules = [
        ("tools.common", "tools.common"),
        ("tools.xapi", "tools.xapi"),
        ("tools.ig_api", "tools.ig_api"),
        ("tools.ig_api", "tools.ig_api"),
        ("config.niches", "config.niches"),
        ("agents.engager", "agents.engager"),
        ("agents.writer", "agents.writer"),
    ]
    failures = []
    for display_name, module_name in modules:
        try:
            __import__(module_name)
        except Exception as e:
            failures.append(f"{display_name}: {e}")

    if failures:
        return False, f"Import failures: {'; '.join(failures)}"
    return True, f"All {len(modules)} modules import OK"


def check_db_integrity() -> tuple[bool, str]:
    """Check SQLite database integrity."""
    issues = []

    db_path = BASE_DIR / "data" / "tatami.db"
    if not db_path.exists():
        return False, "Database file not found"

    try:
        from tools.db import get_db
        db = get_db()

        # Quick integrity check
        result = db.execute("PRAGMA integrity_check").fetchone()
        if result[0] != "ok":
            issues.append(f"DB integrity: {result[0]}")

        # Check posts exist for each niche
        from config.niches import list_niches
        for nid in list_niches():
            row = db.execute(
                "SELECT COUNT(*) as cnt FROM posts WHERE niche_id = ?", (nid,)
            ).fetchone()
            if row["cnt"] == 0:
                issues.append(f"No posts for niche {nid}")

    except Exception as e:
        issues.append(f"DB error: {e}")

    if issues:
        return False, "; ".join(issues)
    return True, "Database OK"


def check_auth_status() -> tuple[bool, str]:
    """Check X API keys and IG token/profile."""
    warnings = []

    # X API OAuth 1.0a keys
    required_keys = ["X_API_CONSUMER_KEY", "X_API_CONSUMER_SECRET", "X_API_ACCESS_TOKEN", "X_API_ACCESS_TOKEN_SECRET"]
    env_file = BASE_DIR / ".env"
    env_text = env_file.read_text() if env_file.exists() else ""
    missing_keys = [k for k in required_keys if k not in env_text and not os.environ.get(k)]
    if missing_keys:
        warnings.append(f"Missing X API keys: {', '.join(missing_keys)}")

    # IG: check for browser profile (primary method) or Graph API token
    ig_token = os.environ.get("IG_ACCESS_TOKEN", "")
    if not ig_token:
        if "IG_ACCESS_TOKEN" not in env_text:
            pass  # Graph API token optional if using browser

    # Playwright browser profile
    profile_dir = DATA_DIR / "ig_browser_profile"
    if not profile_dir.exists():
        warnings.append("IG browser profile missing (run ig_post.py --login)")

    if warnings:
        return False, "; ".join(warnings)
    return True, "Auth OK"


def check_log_sizes() -> tuple[bool, str]:
    """Warn if database is too large."""
    db_path = BASE_DIR / "data" / "tatami.db"
    if not db_path.exists():
        return True, "DB not found"

    size_mb = db_path.stat().st_size / (1024 * 1024)
    if size_mb > 100:
        return False, f"tatami.db: {size_mb:.1f}MB (consider archival)"
    return True, f"DB size OK ({size_mb:.1f}MB)"


def check_posts_queue() -> tuple[bool, str]:
    """Warn if fewer than 3 approved posts remaining in any niche."""
    from config.niches import list_niches
    from tools.db import get_db
    warnings = []
    total_approved = 0
    db = get_db()
    for nid in list_niches():
        row = db.execute(
            "SELECT COUNT(*) as cnt FROM posts WHERE niche_id = ? AND status = 'approved' AND scheduled_for IS NOT NULL",
            (nid,),
        ).fetchone()
        count = row["cnt"]
        total_approved += count
        if count < 3:
            warnings.append(f"{nid}: {count} posts left")

    if warnings:
        return False, "; ".join(warnings)
    return True, f"{total_approved} posts queued across niches"


def check_stale_locks() -> tuple[bool, list[str]]:
    """Detect stale process locks in DB (dead PIDs or old heartbeats)."""
    stale = []
    try:
        from tools.db import get_db
        db = get_db()
        rows = db.execute("SELECT lock_name, pid, heartbeat_at FROM process_locks").fetchall()
        for r in rows:
            pid = r["pid"]
            try:
                os.kill(pid, 0)
                # Process alive — check heartbeat
                if r["heartbeat_at"]:
                    hb = datetime.fromisoformat(r["heartbeat_at"])
                    age_hours = (datetime.now(timezone.utc) - hb).total_seconds() / 3600
                    if age_hours > 2:
                        stale.append(r["lock_name"])
            except ProcessLookupError:
                stale.append(r["lock_name"])
            except PermissionError:
                pass  # Process exists but can't signal — not stale
    except Exception:
        pass

    if stale:
        return False, stale
    return True, []


def check_disk_space() -> tuple[bool, str]:
    """Flag if < 1GB free."""
    usage = shutil.disk_usage(str(BASE_DIR))
    free_gb = usage.free / (1024 ** 3)
    if free_gb < 1.0:
        return False, f"{free_gb:.1f}GB free"
    return True, f"{free_gb:.0f}GB free"


def archive_old_entries(log_path: Path, days: int = 90) -> int:
    """Move log entries older than N days to an archive file."""
    if not log_path.exists():
        return 0

    try:
        entries = json.loads(log_path.read_text())
    except (json.JSONDecodeError, OSError):
        return 0

    if not isinstance(entries, list):
        return 0

    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    keep = []
    archived = []
    for e in entries:
        ts = e.get("timestamp", "")
        if ts and ts < cutoff:
            archived.append(e)
        else:
            keep.append(e)

    if not archived:
        return 0

    # Write archive
    archive_path = log_path.with_suffix(f".archive-{datetime.now().strftime('%Y%m%d')}.json")
    if archive_path.exists():
        existing = json.loads(archive_path.read_text())
        archived = existing + archived
    archive_path.write_text(json.dumps(archived, indent=2, default=str))

    # Write trimmed log (atomic)
    tmp = log_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(keep, indent=2, ensure_ascii=False, default=str))
    tmp.rename(log_path)

    return len(archived) - (len(archived) - len([a for a in archived if a.get("timestamp", "") < cutoff]))


def main():
    parser = argparse.ArgumentParser(description="Health check for content-curator")
    parser.add_argument("--fix", action="store_true", help="Auto-fix: archive old logs, remove stale locks")
    parser.add_argument("--niche", help="Niche name (accepted for orchestrator compatibility, ignored)")
    args = parser.parse_args()

    log.info("Running health check...")

    checks = []

    # 1. Imports
    ok, msg = check_imports()
    checks.append(("Imports", ok, msg))
    log.info(f"  {'OK' if ok else 'WARN'} Imports: {msg}")

    # 2. Database integrity
    ok, msg = check_db_integrity()
    checks.append(("DB", ok, msg))
    log.info(f"  {'OK' if ok else 'WARN'} DB: {msg}")

    # 3. Auth status
    ok, msg = check_auth_status()
    checks.append(("Auth", ok, msg))
    log.info(f"  {'OK' if ok else 'WARN'} Auth: {msg}")

    # 4. DB size
    ok, msg = check_log_sizes()
    checks.append(("Size", ok, msg))
    log.info(f"  {'OK' if ok else 'WARN'} Size: {msg}")

    # 5. Posts queue
    ok, msg = check_posts_queue()
    checks.append(("Queue", ok, msg))
    log.info(f"  {'OK' if ok else 'WARN'} Queue: {msg}")

    # 6. Stale process locks
    ok, stale_locks = check_stale_locks()
    if stale_locks:
        msg = f"Stale: {', '.join(stale_locks)}"
        if args.fix:
            from tools.db import get_db as _fix_db
            db = _fix_db()
            for lock_name in stale_locks:
                db.execute("DELETE FROM process_locks WHERE lock_name = ?", (lock_name,))
            db.commit()
            log.info(f"  Removed {len(stale_locks)} stale process locks")
            msg += " (removed)"
    else:
        msg = "No stale locks"
    checks.append(("Locks", ok, msg))
    log.info(f"  {'OK' if ok else 'WARN'} Locks: {msg}")

    # 7. Disk space
    ok, msg = check_disk_space()
    checks.append(("Disk", ok, msg))
    log.info(f"  {'OK' if ok else 'WARN'} Disk: {msg}")

    # Summary
    passed = sum(1 for _, ok, _ in checks if ok)
    total = len(checks)
    warnings = [f"{name}: {msg}" for name, ok, msg in checks if not ok]

    summary = f"Health: {passed}/{total} OK"
    if warnings:
        summary += ". WARN: " + "; ".join(warnings)

    log.info(f"\n  {summary}")
    notify("health check", summary, priority="default" if passed == total else "high")


if __name__ == "__main__":
    main()
