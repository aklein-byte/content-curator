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
        ("tools.ig_browser", "tools.ig_browser"),
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


def check_json_files() -> tuple[bool, str]:
    """Load all data files and verify structure."""
    issues = []

    # Check posts files for all niches
    from config.niches import list_niches, get_niche
    for nid in list_niches():
        ncfg = get_niche(nid)
        pf_name = ncfg.get("posts_file", "posts.json")
        pf = BASE_DIR / pf_name
        if pf.exists():
            try:
                data = json.loads(pf.read_text())
                if "posts" not in data:
                    issues.append(f"{pf_name} missing 'posts' key")
                elif not isinstance(data["posts"], list):
                    issues.append(f"{pf_name} 'posts' is not a list")
            except json.JSONDecodeError as e:
                issues.append(f"{pf_name} corrupt: {e}")

    # Log files should be arrays (check all niche variants)
    log_files = [
        "engagement-log.json",
        "ig-engagement-log.json",
        "response-log.json",
        "thread-log.json",
    ]
    for nid in list_niches():
        if nid != "tatamispaces":
            log_files.extend([
                f"engagement-log-{nid}.json",
                f"ig-engagement-log-{nid}.json",
            ])
    for lf in log_files:
        path = BASE_DIR / lf
        if path.exists():
            try:
                data = json.loads(path.read_text())
                if not isinstance(data, list):
                    issues.append(f"{lf} is not a list")
            except json.JSONDecodeError as e:
                issues.append(f"{lf} corrupt: {e}")

    # orchestrator status
    status_file = DATA_DIR / "orchestrator-status.json"
    if status_file.exists():
        try:
            json.loads(status_file.read_text())
        except json.JSONDecodeError as e:
            issues.append(f"orchestrator-status.json corrupt: {e}")

    if issues:
        return False, "; ".join(issues)
    return True, "All JSON files OK"


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
    """Warn if engagement logs are too large."""
    warnings = []
    from config.niches import list_niches
    log_files = [
        "engagement-log.json",
        "ig-engagement-log.json",
        "response-log.json",
    ]
    for nid in list_niches():
        if nid != "tatamispaces":
            log_files.extend([
                f"engagement-log-{nid}.json",
                f"ig-engagement-log-{nid}.json",
            ])
    for lf in log_files:
        path = BASE_DIR / lf
        if path.exists():
            size_mb = path.stat().st_size / (1024 * 1024)
            if size_mb > 10:
                warnings.append(f"{lf}: {size_mb:.1f}MB (consider archival)")

    if warnings:
        return False, "; ".join(warnings)
    return True, "Log sizes OK"


def check_posts_queue() -> tuple[bool, str]:
    """Warn if fewer than 3 approved posts remaining in any niche."""
    from config.niches import list_niches, get_niche
    warnings = []
    total_approved = 0
    for nid in list_niches():
        ncfg = get_niche(nid)
        posts_file = BASE_DIR / ncfg.get("posts_file", "posts.json")
        if not posts_file.exists():
            continue
        try:
            data = json.loads(posts_file.read_text())
        except json.JSONDecodeError:
            warnings.append(f"{nid}: corrupt")
            continue
        approved = [
            p for p in data.get("posts", [])
            if p.get("status") == "approved" and p.get("scheduled_for")
        ]
        total_approved += len(approved)
        if len(approved) < 3:
            warnings.append(f"{nid}: {len(approved)} posts left")

    if warnings:
        return False, "; ".join(warnings)
    return True, f"{total_approved} posts queued across niches"


def check_stale_lockfiles() -> tuple[bool, list[Path]]:
    """Detect lockfiles older than 2 hours (likely from crashed processes)."""
    stale = []
    for lock in BASE_DIR.glob(".*.lock"):
        age_hours = (datetime.now() - datetime.fromtimestamp(lock.stat().st_mtime)).total_seconds() / 3600
        if age_hours > 2:
            stale.append(lock)

    if stale:
        names = [l.name for l in stale]
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

    # 2. JSON integrity
    ok, msg = check_json_files()
    checks.append(("JSON", ok, msg))
    log.info(f"  {'OK' if ok else 'WARN'} JSON: {msg}")

    # 3. Auth status
    ok, msg = check_auth_status()
    checks.append(("Auth", ok, msg))
    log.info(f"  {'OK' if ok else 'WARN'} Auth: {msg}")

    # 4. Log sizes
    ok, msg = check_log_sizes()
    checks.append(("Logs", ok, msg))
    log.info(f"  {'OK' if ok else 'WARN'} Logs: {msg}")

    # 5. Posts queue
    ok, msg = check_posts_queue()
    checks.append(("Queue", ok, msg))
    log.info(f"  {'OK' if ok else 'WARN'} Queue: {msg}")

    # 6. Stale lockfiles
    ok, stale_locks = check_stale_lockfiles()
    if stale_locks:
        lock_names = [l.name for l in stale_locks]
        msg = f"Stale: {', '.join(lock_names)}"
        if args.fix:
            for l in stale_locks:
                l.unlink()
                log.info(f"  Removed stale lockfile: {l.name}")
            msg += " (removed)"
    else:
        msg = "No stale locks"
    checks.append(("Locks", ok, msg))
    log.info(f"  {'OK' if ok else 'WARN'} Locks: {msg}")

    # 7. Disk space
    ok, msg = check_disk_space()
    checks.append(("Disk", ok, msg))
    log.info(f"  {'OK' if ok else 'WARN'} Disk: {msg}")

    # Auto-fix: archive old log entries
    if args.fix:
        from config.niches import list_niches as _ln
        archive_files = ["engagement-log.json", "ig-engagement-log.json", "response-log.json"]
        for nid in _ln():
            if nid != "tatamispaces":
                archive_files.extend([
                    f"engagement-log-{nid}.json",
                    f"ig-engagement-log-{nid}.json",
                ])
        for lf in archive_files:
            path = BASE_DIR / lf
            count = archive_old_entries(path, days=90)
            if count > 0:
                log.info(f"  Archived {count} entries from {lf}")

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
