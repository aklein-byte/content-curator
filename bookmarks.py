"""
Bookmark-to-post pipeline for @tatamispaces.
Fetches your X bookmarks, evaluates them, drafts captions,
and adds them to posts.json as drafts for review.

Usage: python bookmarks.py [--niche tatamispaces] [--max-drafts 10]
"""

import sys
import os
import json
import asyncio
import argparse
from pathlib import Path
from datetime import datetime, timezone, timedelta

sys.path.insert(0, str(Path(__file__).parent))

from dotenv import load_dotenv
load_dotenv()

from tools.xapi import get_bookmarks, XPost, set_niche as set_xapi_niche
from tools.common import load_json, save_json, notify, acquire_lock, release_lock, setup_logging, load_config
from agents.engager import evaluate_post, draft_original_post
from config.niches import get_niche

log = setup_logging("bookmarks")

BASE_DIR = Path(__file__).parent
POSTS_FILE: Path = None  # resolved in main() from niche config

_pw = load_config().get("posting_window", {})


def load_posts() -> dict:
    return load_json(POSTS_FILE, default={"posts": []})


def save_posts(data: dict):
    save_json(POSTS_FILE, data)


def next_post_id(posts_data: dict) -> int:
    existing_ids = [p.get("id", 0) for p in posts_data.get("posts", [])]
    return max(existing_ids, default=0) + 1


MAX_PER_DAY = _pw.get("max_per_day", 4)
MIN_GAP_HOURS = _pw.get("min_gap_hours", 2)
WINDOW_START = _pw.get("start_hour_et", 7)
WINDOW_END = _pw.get("end_hour_et", 22)


def next_schedule_slot(posts_data: dict) -> datetime:
    """Find the next available posting slot with random timing across the day."""
    import random
    from zoneinfo import ZoneInfo
    et = ZoneInfo("America/New_York")

    # Collect all scheduled times as datetimes
    taken_times = []
    for p in posts_data.get("posts", []):
        sf = p.get("scheduled_for")
        if sf and p.get("status") in ("approved", "posted", "scheduled_native"):
            try:
                taken_times.append(datetime.fromisoformat(sf).astimezone(et))
            except Exception:
                pass

    now_et = datetime.now(et)
    check_date = now_et.date()
    if now_et.hour >= WINDOW_END:
        check_date += timedelta(days=1)

    for day_offset in range(30):
        d = check_date + timedelta(days=day_offset)

        posts_on_day = sum(1 for t in taken_times if t.date() == d)
        if posts_on_day >= MAX_PER_DAY:
            continue

        # Try random times within the window, up to 20 attempts
        for _ in range(20):
            hour = random.randint(WINDOW_START, WINDOW_END - 1)
            minute = random.randint(0, 59)
            candidate = datetime(d.year, d.month, d.day, hour, minute, tzinfo=et)

            if candidate <= now_et:
                continue

            too_close = any(
                abs((candidate - t).total_seconds()) < MIN_GAP_HOURS * 3600
                for t in taken_times
            )
            if too_close:
                continue

            return candidate

    return datetime.now(et) + timedelta(days=30)


def already_in_queue(posts_data: dict, post_id: str) -> bool:
    """Check if a post ID or source URL containing it is already queued."""
    for p in posts_data.get("posts", []):
        src = p.get("source_url") or ""
        if post_id in src:
            return True
    return False


async def main():
    parser = argparse.ArgumentParser(description="Turn bookmarks into post drafts")
    parser.add_argument("--niche", default="tatamispaces", help="Niche ID")
    parser.add_argument("--max-drafts", type=int, default=10, help="Max new drafts to create")
    parser.add_argument("--min-score", type=int, default=7, help="Minimum relevance score (1-10)")
    args = parser.parse_args()

    niche_id = args.niche
    niche = get_niche(niche_id)

    # Resolve niche-aware posts file
    global POSTS_FILE
    POSTS_FILE = Path(os.environ.get("POSTS_FILE", str(BASE_DIR / niche.get("posts_file", "posts.json"))))

    log.info(f"Fetching bookmarks for {niche['handle']}")

    # Set API credentials for this niche
    set_xapi_niche(niche_id)

    # Fetch bookmarks via official API v2 (OAuth 2.0)
    bookmark_posts = get_bookmarks(max_results=40)
    log.info(f"Got {len(bookmark_posts)} bookmarks")

    # Filter: must have images
    with_images = [p for p in bookmark_posts if len(p.image_urls) > 0]
    log.info(f"{len(with_images)} have images")

    # Load existing posts to skip duplicates
    posts_data = load_posts()

    # Evaluate and draft
    drafts_created = 0
    skipped = 0

    for post in with_images:
        if drafts_created >= args.max_drafts:
            break

        if already_in_queue(posts_data, post.post_id):
            skipped += 1
            continue

        # Evaluate relevance
        evaluation = await evaluate_post(
            post_text=post.text,
            author=post.author_handle,
            niche_id=niche_id,
            image_count=len(post.image_urls),
            likes=post.likes,
            reposts=post.reposts,
        )

        try:
            score = int(evaluation.get("relevance_score") or 0)
        except (TypeError, ValueError):
            score = 0
        reason = evaluation.get("reason", "no reason") or "no reason"
        if score < args.min_score:
            log.info(f"  Skip @{post.author_handle} — score {score}/10: {reason[:50]}")
            continue

        log.info(f"  @{post.author_handle} — score {score}/10, {post.likes} likes")

        # Draft caption
        caption_data = await draft_original_post(
            source_text=post.text,
            author=post.author_handle,
            niche_id=niche_id,
        )

        caption_text = caption_data.get("text", "")
        if not caption_text:
            log.warning(f"  Empty caption for @{post.author_handle}, skipping")
            continue

        if len(caption_text) > 4000:
            log.warning(f"  Caption too long ({len(caption_text)} chars) for @{post.author_handle}, skipping")
            continue

        source_url = f"https://x.com/{post.author_handle}/status/{post.post_id}"
        sched = next_schedule_slot(posts_data)

        new_post = {
            "id": next_post_id(posts_data),
            "type": "repost-with-credit",
            "text": caption_text,
            "image": None,
            "image_urls": post.image_urls,
            "source_url": source_url,
            "source_handle": f"@{post.author_handle}",
            "status": "approved",
            "score": score,
            "scheduled_for": sched.isoformat(),
            "notes": f"From bookmarks. {post.likes} likes. {evaluation['reason'][:80]}",
        }

        posts_data["posts"].append(new_post)
        drafts_created += 1
        log.info(f"  Scheduled #{new_post['id']} for {sched.strftime('%b %d %I%p ET')}: {caption_text[:60]}...")

    save_posts(posts_data)

    # Summary
    print()
    print("=" * 60)
    print(f"BOOKMARKS PROCESSED for {niche['handle']}")
    print("=" * 60)
    print(f"Bookmarks fetched:  {len(bookmark_posts)}")
    print(f"With images:        {len(with_images)}")
    print(f"Already in queue:   {skipped}")
    print(f"New drafts created: {drafts_created}")
    print()

    if drafts_created > 0:
        print("New drafts:")
        for p in posts_data["posts"]:
            if p.get("status") == "draft":
                print(f"  #{p['id']} — {p['text'][:70]}...")
                print(f"          from {p.get('source_handle', '?')}")
        print()
        print(f"Review in {POSTS_FILE}")
        print("Change status to 'approved' and add 'scheduled_for' to publish.")

    if drafts_created > 0:
        notify(f"{niche['handle']} bookmarks", f"{drafts_created} new drafts from bookmarks")


if __name__ == "__main__":
    lock_fd = acquire_lock(BASE_DIR / ".bookmarks.lock")
    if not lock_fd:
        log.info("Another bookmarks.py is already running, exiting")
        sys.exit(0)
    try:
        asyncio.run(main())
    finally:
        release_lock(lock_fd)
