"""
Instagram cross-posting script — niche-agnostic.
Reads posts file, finds recently posted X tweets that haven't been cross-posted,
and posts to Instagram via the Instagram Graph API.

Supports both flat (tatami) and museum post formats.

Usage: python ig_post.py [--niche tatamispaces] [--dry-run] [--max 3]
"""

import sys
import os
import json
import asyncio
import argparse
from pathlib import Path
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).parent))

from dotenv import load_dotenv
load_dotenv()

from config.niches import get_niche
from tools.ig_api import publish_single, publish_carousel
from tools.ig_browser import adapt_caption_for_ig
from tools.common import load_json, save_json, notify, acquire_lock, release_lock, setup_logging

log = setup_logging("ig_post")

BASE_DIR = Path(__file__).parent

# Resolved per-niche at runtime
_resolved_posts_file: Path = Path(os.environ.get("POSTS_FILE", str(BASE_DIR / "posts.json")))
_resolved_ig_log: Path = BASE_DIR / "ig-post-log.json"


def _init_paths(niche: dict):
    """Set posts file and IG log paths based on niche config."""
    global _resolved_posts_file, _resolved_ig_log
    posts_filename = niche.get("posts_file", "posts.json")
    _resolved_posts_file = Path(os.environ.get("POSTS_FILE", str(BASE_DIR / posts_filename)))
    # Separate IG log per niche to avoid cross-contamination
    niche_name = niche.get("name", "").lower().replace(" ", "")
    if niche_name and niche_name != "tatami":
        _resolved_ig_log = BASE_DIR / f"ig-post-log-{niche_name}.json"
    else:
        _resolved_ig_log = BASE_DIR / "ig-post-log.json"


def load_posts() -> dict:
    return load_json(_resolved_posts_file, default={"posts": []})


def save_posts(data: dict):
    save_json(_resolved_posts_file, data, lock=True)


def count_ig_posts_today(posts_data: dict) -> int:
    """Count how many posts were cross-posted to IG today."""
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    count = 0
    for post in posts_data.get("posts", []):
        ig_at = post.get("ig_posted_at", "")
        if ig_at and today_str in ig_at:
            count += 1
    return count


IG_MAX_PER_DAY = 3


def _load_ig_log() -> list:
    """Load the separate IG post log (dedup source of truth)."""
    return load_json(_resolved_ig_log, default=[])


def _save_ig_log(entries: list):
    save_json(_resolved_ig_log, entries)


def _ig_already_posted(ig_log: list, post_id) -> bool:
    """Check if a post ID has already been cross-posted to IG (via separate log)."""
    return any(e.get("post_id") == post_id for e in ig_log)


def find_unposted_to_ig(posts_data: dict, ig_log: list, max_count: int = 3) -> list[dict]:
    """Find posts that are posted to X but not yet to Instagram.
    Uses both posts.json flags AND the separate IG log for dedup safety.
    """
    ready = []
    for post in posts_data.get("posts", []):
        if post.get("status") != "posted":
            continue
        # Check posts.json flags
        if post.get("ig_posted") or post.get("ig_container_created"):
            continue
        # Check separate dedup log (survives posts.json clobbers)
        if _ig_already_posted(ig_log, post.get("id")):
            continue
        if not post.get("image") and not post.get("image_urls") and not post.get("allImages"):
            continue
        ready.append(post)
        if len(ready) >= max_count:
            break
    return ready



def _resolve_image_urls(post: dict) -> list[str]:
    """Get publishable HTTPS image URLs for a post.

    Supports two formats:
      - Flat (tatami): image_urls list
      - Museum: allImages list + tweets[0].images indices

    Uses original source URLs (e.g. Twitter CDN, museum APIs) directly since
    they're already HTTPS and publicly accessible.
    """
    urls = post.get("image_urls", [])

    # Museum format: resolve from allImages + first tweet's image indices
    if not urls and post.get("allImages"):
        all_imgs = post["allImages"]
        tweets = post.get("tweets", [])
        if tweets and tweets[0].get("images"):
            urls = [all_imgs[i] for i in tweets[0]["images"] if i < len(all_imgs)]
        else:
            # Fallback: use first image from allImages
            urls = [all_imgs[0]] if all_imgs else []

    if not urls:
        return []

    # Dashboard single-image selection
    idx = post.get("image_index")
    if idx is not None and 0 <= idx < len(urls):
        return [urls[idx]]

    return urls




async def main():
    parser = argparse.ArgumentParser(description="Cross-post to Instagram")
    parser.add_argument("--niche", default="tatamispaces", help="Niche ID")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be posted")
    parser.add_argument("--max", type=int, default=3, help="Max posts to cross-post")
    args = parser.parse_args()

    niche_id = args.niche
    niche = get_niche(niche_id)

    _init_paths(niche)
    ig_env = niche.get("ig_env")
    log.info(f"Instagram cross-post for {niche['handle']} ({'DRY RUN' if args.dry_run else 'LIVE'})")

    posts_data = load_posts()

    # Daily limit — prevent runaway posting
    if not args.dry_run:
        today_count = count_ig_posts_today(posts_data)
        if today_count >= IG_MAX_PER_DAY:
            log.info(f"Daily IG limit reached ({today_count}/{IG_MAX_PER_DAY}), skipping")
            return
        remaining = IG_MAX_PER_DAY - today_count
        if args.max > remaining:
            args.max = remaining
            log.info(f"Limiting to {remaining} post(s) (daily max {IG_MAX_PER_DAY})")

    ig_log = _load_ig_log()
    to_post = find_unposted_to_ig(posts_data, ig_log, max_count=args.max)

    if not to_post:
        log.info("No posts ready for Instagram cross-posting")
        return

    log.info(f"Found {len(to_post)} post(s) to cross-post")

    if args.dry_run:
        for post in to_post:
            post_text = post.get("text") or post.get("tweets", [{}])[0].get("text", "")
            caption = adapt_caption_for_ig(post_text, niche)
            image_urls = _resolve_image_urls(post)
            log.info(f"  [DRY RUN] #{post['id']}: {caption[:80]}...")
            log.info(f"    Images ({len(image_urls)}): {image_urls}")
        return

    # Publish via Graph API
    posted_count = 0
    for post in to_post:
        image_urls = _resolve_image_urls(post)
        if not image_urls:
            log.warning(f"  Skip #{post['id']} — no images")
            continue

        # Re-read posts.json before each post to catch if another process already posted it
        fresh_data = load_posts()
        fresh_post = next((p for p in fresh_data.get("posts", []) if p.get("id") == post.get("id")), None)
        if fresh_post and fresh_post.get("ig_posted"):
            log.info(f"  Skip #{post['id']} — already cross-posted (detected on re-read)")
            continue

        post_text = post.get("text") or post.get("tweets", [{}])[0].get("text", "")
        caption = adapt_caption_for_ig(post_text, niche)
        log.info(f"Posting #{post['id']} ({len(image_urls)} image{'s' if len(image_urls) > 1 else ''})...")

        # Mark container as created BEFORE calling the API.
        # This prevents duplicates: if the publish call fails with 403,
        # IG containers auto-publish anyway, so we must never create
        # containers for the same post twice.
        post["ig_container_created"] = True
        post["ig_container_created_at"] = datetime.now(timezone.utc).isoformat()
        save_posts(posts_data)

        try:
            if len(image_urls) == 1:
                result = publish_single(image_urls[0], caption, ig_env=ig_env)
            else:
                result = publish_carousel(image_urls, caption, ig_env=ig_env)

            now = datetime.now(timezone.utc).isoformat()
            post["ig_posted"] = True
            post["ig_posted_at"] = now
            post["ig_media_id"] = result.get("id")
            post["ig_images_attempted"] = len(image_urls)
            posted_count += 1
            log.info(f"  Posted #{post['id']} to Instagram (media_id: {result.get('id')})")
            save_posts(posts_data)
            # Write to separate dedup log (survives posts.json race conditions)
            ig_log.append({"post_id": post.get("id"), "ig_media_id": result.get("id"), "timestamp": now})
            _save_ig_log(ig_log)
        except Exception as e:
            # Container was already created — mark as posted anyway since
            # IG containers auto-publish even when media_publish returns 403
            now = datetime.now(timezone.utc).isoformat()
            post["ig_posted"] = True
            post["ig_posted_at"] = now
            post["ig_publish_error"] = str(e)
            save_posts(posts_data)
            ig_log.append({"post_id": post.get("id"), "error": str(e), "timestamp": now})
            _save_ig_log(ig_log)
            log.error(f"  Failed #{post['id']}: {e}")
            log.warning(f"  Marked as ig_posted anyway (containers auto-publish)")

    summary = f"Instagram: {posted_count} post(s) cross-posted"
    log.info(summary)
    if posted_count > 0:
        notify(f"{niche['handle']} IG", summary)


if __name__ == "__main__":
    lock_fd = acquire_lock(BASE_DIR / ".ig_post.lock")
    if not lock_fd:
        log.info("Another ig_post.py is already running, exiting")
        sys.exit(0)
    try:
        asyncio.run(main())
    finally:
        release_lock(lock_fd)
