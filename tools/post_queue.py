"""
Shared post queue management for all content pipelines.

Consolidates load/save/id/schedule/path-resolution that was duplicated
across bookmarks.py, museum_fetch.py, post.py, ig_post.py, engage.py,
and dashboard.py.

Every function takes niche_id explicitly — no module-level globals.

Storage: SQLite via tools.db (was JSON files).
"""

import os
import random
from pathlib import Path
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from tools.db import (
    get_db, init_db,
    get_all_posts as _db_get_all_posts,
    get_post as _db_get_post,
    insert_post as _db_insert_post,
    update_post as _db_update_post,
    json_dumps,
)
from tools.common import load_config
from config.niches import get_niche

BASE_DIR = Path(__file__).parent.parent
ET = ZoneInfo("America/New_York")

# Ensure DB is initialized on import
init_db()


def resolve_posts_file(niche_id: str) -> Path:
    """Resolve the posts JSON file path for a niche.

    Kept for backward compatibility (used by dashboard template rendering
    and any code that references the filename for display). Does NOT affect
    storage — all data is in SQLite now.
    """
    env_file = os.environ.get("POSTS_FILE")
    if env_file:
        return Path(env_file)
    niche = get_niche(niche_id)
    filename = niche.get("posts_file", "posts.json")
    return BASE_DIR / filename


def load_posts(niche_id: str) -> dict:
    """Load posts data for a niche. Returns {"posts": [...]}.

    Backward compatible: returns the same dict structure all scripts expect.
    """
    posts = _db_get_all_posts(niche_id)
    return {"posts": posts}


def save_posts(data: dict, niche_id: str, lock: bool = False) -> None:
    """Save posts data for a niche.

    Diff-based: compares incoming dict against DB state and applies
    INSERT/UPDATE as needed. All writes happen in a single transaction.
    The `lock` parameter is ignored (SQLite handles concurrency).
    """
    db = get_db()
    incoming_posts = data.get("posts", [])

    # Build a map of existing DB posts for this niche
    existing_rows = db.execute(
        "SELECT id FROM posts WHERE niche_id = ?", (niche_id,)
    ).fetchall()
    existing_ids = {r["id"] for r in existing_rows}

    for post in incoming_posts:
        post_id = post.get("id")
        if post_id is None:
            _db_insert_post(niche_id, post, _commit=False)
        elif post_id in existing_ids:
            fields = {k: v for k, v in post.items() if k not in ("id", "niche_id")}
            _db_update_post(niche_id, post_id, _commit=False, **fields)
        else:
            _db_insert_post(niche_id, post, _commit=False)

    db.commit()


def next_post_id(posts_data: dict | None = None, niche_id: str | None = None) -> int:
    """Return the next available post ID (max existing + 1).

    Accepts either the old-style dict or a niche_id for direct DB query.
    """
    if niche_id:
        db = get_db()
        row = db.execute(
            "SELECT COALESCE(MAX(id), 0) + 1 AS next_id FROM posts WHERE niche_id = ?",
            (niche_id,),
        ).fetchone()
        return row["next_id"]

    # Backward compat: compute from dict
    if posts_data:
        existing = [p.get("id", 0) for p in posts_data.get("posts", [])]
        return max(existing, default=0) + 1

    return 1


def already_in_queue(posts_data: dict | None = None, identifier: str = "",
                     niche_id: str | None = None) -> bool:
    """Check if a URL fragment is already in the queue.

    Can use either old-style dict or direct DB query via niche_id.
    """
    if niche_id:
        db = get_db()
        row = db.execute(
            "SELECT 1 FROM posts WHERE niche_id = ? AND source_url LIKE ? LIMIT 1",
            (niche_id, f"%{identifier}%"),
        ).fetchone()
        return row is not None

    # Backward compat: search dict
    if posts_data:
        for p in posts_data.get("posts", []):
            src = p.get("source_url") or ""
            if identifier in src:
                return True
    return False


def images_already_in_queue(image_urls: list[str], niche_id: str) -> bool:
    """Check if any image URL (base, without query params) already exists in the queue.

    Catches re-bookmarked content where the tweet ID differs but images are identical.
    """
    if not image_urls:
        return False
    db = get_db()
    for url in image_urls[:4]:
        base = url.split("?")[0] if "?" in url else url
        if not base:
            continue
        row = db.execute(
            "SELECT 1 FROM posts WHERE niche_id = ? AND image_urls LIKE ? LIMIT 1",
            (niche_id, f"%{base}%"),
        ).fetchone()
        if row:
            return True
    return False


# --- Granular helpers for targeted operations ---

def update_post(niche_id: str, post_id: int, **fields):
    """Update specific fields on a single post. No full-file rewrite."""
    _db_update_post(niche_id, post_id, **fields)


def insert_post(niche_id: str, post_dict: dict) -> int:
    """Insert a new post and return its assigned ID."""
    return _db_insert_post(niche_id, post_dict)


def get_post(niche_id: str, post_id: int) -> dict | None:
    """Fetch a single post by ID."""
    return _db_get_post(niche_id, post_id)


# --- Scheduling ---

def next_schedule_slot(posts_data: dict, niche_id: str,
                       mode: str = "auto") -> datetime | str:
    """Find the next available posting slot.

    Two scheduling modes (auto-detected from niche config):

    "fixed" — Fixed posting_times with jitter (museum-style).
        Uses engagement.posting_times from niche config.
        Returns ISO string.

    "random" — Random time within a daily window (bookmarks-style).
        Uses posting_window from config JSON.
        Returns datetime with ET timezone.

    mode="auto" picks "fixed" if niche has posting_times, else "random".
    """
    niche = get_niche(niche_id)
    posting_times = niche.get("engagement", {}).get("posting_times", [])

    if mode == "auto":
        mode = "fixed" if posting_times else "random"

    if mode == "fixed":
        return _next_fixed_slot(niche_id, posting_times)
    else:
        return _next_random_slot(niche_id)


def _next_fixed_slot(niche_id: str, posting_times: list[str]) -> str:
    """Fixed posting times with jitter (museum-style scheduling)."""
    if not posting_times:
        posting_times = ["11:00", "18:00"]

    # Query scheduled times directly from DB
    db = get_db()
    rows = db.execute(
        "SELECT scheduled_for FROM posts WHERE niche_id = ? AND status IN ('approved', 'draft') AND scheduled_for IS NOT NULL",
        (niche_id,),
    ).fetchall()
    scheduled = {r["scheduled_for"][:16] for r in rows}

    now = datetime.now(ET)
    candidate = now

    for _ in range(100):
        for time_str in posting_times:
            h, m = time_str.split(":")
            base_slot = candidate.replace(
                hour=int(h), minute=int(m), second=0, microsecond=0
            )
            jitter = random.randint(-30, 30)
            slot = base_slot + timedelta(minutes=jitter)

            if slot <= now:
                continue

            slot_key = slot.isoformat()[:16]
            if slot_key not in scheduled:
                return slot.isoformat()

        candidate += timedelta(days=1)

    return (now + timedelta(days=1)).isoformat()


def _next_random_slot(niche_id: str) -> datetime:
    """Random time within a daily window (bookmarks-style scheduling)."""
    pw = load_config().get("posting_window", {})
    max_per_day = pw.get("max_per_day", 4)
    min_gap_hours = pw.get("min_gap_hours", 2)
    window_start = pw.get("start_hour_et", 7)
    window_end = pw.get("end_hour_et", 22)

    # Query taken times from DB
    db = get_db()
    rows = db.execute(
        "SELECT scheduled_for FROM posts WHERE niche_id = ? AND status IN ('approved', 'posted', 'scheduled_native') AND scheduled_for IS NOT NULL",
        (niche_id,),
    ).fetchall()

    taken_times = []
    for r in rows:
        try:
            taken_times.append(datetime.fromisoformat(r["scheduled_for"]).astimezone(ET))
        except Exception:
            pass

    now_et = datetime.now(ET)
    check_date = now_et.date()
    if now_et.hour >= window_end:
        check_date += timedelta(days=1)

    for day_offset in range(30):
        d = check_date + timedelta(days=day_offset)
        posts_on_day = sum(1 for t in taken_times if t.date() == d)
        if posts_on_day >= max_per_day:
            continue

        for _ in range(20):
            hour = random.randint(window_start, window_end - 1)
            minute = random.randint(0, 59)
            candidate = datetime(d.year, d.month, d.day, hour, minute, tzinfo=ET)

            if candidate <= now_et:
                continue

            too_close = any(
                abs((candidate - t).total_seconds()) < min_gap_hours * 3600
                for t in taken_times
            )
            if too_close:
                continue

            return candidate

    return datetime.now(ET) + timedelta(days=30)
