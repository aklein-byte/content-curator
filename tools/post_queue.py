"""
Shared post queue management for all content pipelines.

Consolidates load/save/id/schedule/path-resolution that was duplicated
across bookmarks.py, museum_fetch.py, post.py, ig_post.py, engage.py,
and dashboard.py.

Every function takes niche_id explicitly — no module-level globals.
"""

import os
import random
from pathlib import Path
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from tools.common import load_json, save_json, load_config
from config.niches import get_niche

BASE_DIR = Path(__file__).parent.parent
ET = ZoneInfo("America/New_York")


def resolve_posts_file(niche_id: str) -> Path:
    """Resolve the posts JSON file path for a niche.

    Checks POSTS_FILE env var first, falls back to niche config.
    """
    env_file = os.environ.get("POSTS_FILE")
    if env_file:
        return Path(env_file)
    niche = get_niche(niche_id)
    filename = niche.get("posts_file", "posts.json")
    return BASE_DIR / filename


def load_posts(niche_id: str) -> dict:
    """Load posts data for a niche. Returns {"posts": []} if missing."""
    path = resolve_posts_file(niche_id)
    data = load_json(path, default={"posts": []})
    # Normalize: some old files are bare lists
    if isinstance(data, list):
        data = {"posts": data}
    return data


def save_posts(data: dict, niche_id: str, lock: bool = False) -> None:
    """Atomic save of posts data for a niche."""
    path = resolve_posts_file(niche_id)
    save_json(path, data, lock=lock)


def next_post_id(posts_data: dict) -> int:
    """Return the next available post ID (max existing + 1)."""
    existing = [p.get("id", 0) for p in posts_data.get("posts", [])]
    return max(existing, default=0) + 1


def already_in_queue(posts_data: dict, identifier: str) -> bool:
    """Check if a post ID or URL fragment is already in the queue.

    Searches source_url fields for the identifier string.
    """
    for p in posts_data.get("posts", []):
        src = p.get("source_url") or ""
        if identifier in src:
            return True
    return False


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
        return _next_fixed_slot(posts_data, niche_id, posting_times)
    else:
        return _next_random_slot(posts_data, niche_id)


def _next_fixed_slot(posts_data: dict, niche_id: str,
                     posting_times: list[str]) -> str:
    """Fixed posting times with jitter (museum-style scheduling)."""
    if not posting_times:
        posting_times = ["11:00", "18:00"]

    scheduled = set()
    for p in posts_data.get("posts", []):
        sf = p.get("scheduled_for")
        if sf and p.get("status") in ("approved", "draft"):
            scheduled.add(sf[:16])

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


def _next_random_slot(posts_data: dict, niche_id: str) -> datetime:
    """Random time within a daily window (bookmarks-style scheduling)."""
    pw = load_config().get("posting_window", {})
    max_per_day = pw.get("max_per_day", 4)
    min_gap_hours = pw.get("min_gap_hours", 2)
    window_start = pw.get("start_hour_et", 7)
    window_end = pw.get("end_hour_et", 22)

    taken_times = []
    for p in posts_data.get("posts", []):
        sf = p.get("scheduled_for")
        if sf and p.get("status") in ("approved", "posted", "scheduled_native"):
            try:
                taken_times.append(datetime.fromisoformat(sf).astimezone(ET))
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
