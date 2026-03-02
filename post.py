"""
Posting script — niche-agnostic.
Reads posts file, finds the next approved+scheduled post whose time has passed,
downloads the image, posts via X API, and marks it as posted.

Supports two post formats:
  1. Flat (tatami): single text + image_urls, thread via X API v2
  2. Museum: pre-written tweets array with per-tweet images, thread via X API v2

Usage: python post.py [--niche tatamispaces] [--dry-run] [--no-ig] [--post-id 30]
"""

import sys
import os
import re
import json
import asyncio
import argparse
from pathlib import Path
from datetime import datetime, timezone

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent))

from dotenv import load_dotenv
load_dotenv()

from tools.xapi import (
    create_tweet, upload_media, get_own_recent_tweets, set_niche as set_xapi_niche,
    post_thread, download_image,
)
from tools.bluesky import (
    set_niche as set_bsky_niche,
    create_post as bsky_create_post,
    post_thread as bsky_post_thread,
    count_graphemes,
)
from tools.common import notify, setup_logging, load_config, get_model
from tools.db import acquire_process_lock, release_process_lock
from tools.post_queue import resolve_posts_file, load_posts as pq_load_posts, save_posts as pq_save_posts
from agents.writer import generate_thread_captions
from config.niches import get_niche

log = setup_logging("post")

BASE_DIR = Path(__file__).parent
IMAGES_DIR = BASE_DIR / "data" / "images"

# Posting limits — loaded from config, with sensible defaults
_posting_cfg = load_config().get("posting_window", {})
MAX_POSTS_PER_DAY = _posting_cfg.get("max_per_day", 3)
MIN_GAP_HOURS = _posting_cfg.get("min_gap_hours", 2)
MIN_POST_SCORE = _posting_cfg.get("min_post_score", 7)
LOW_QUEUE_THRESHOLD = _posting_cfg.get("low_queue_threshold", 5)
MIN_IMAGE_DIMENSION = _posting_cfg.get("min_image_dimension", 800)


def _strip_mentions(text: str) -> str:
    """Strip @ from @mentions to avoid X API 403 on mention restrictions.

    Converts '📷 @SciFiArchives' to '📷 SciFiArchives'.
    Only strips the @ symbol, preserves the handle text.
    """
    return re.sub(r'@(\w+)', r'\1', text)

# Set once by main(), used by module-level load/save wrappers
_niche_id: str | None = None


def load_posts() -> dict:
    return pq_load_posts(_niche_id)


def save_posts(data: dict):
    pq_save_posts(data, _niche_id, lock=True)


def parse_time(ts: str) -> datetime:
    """Parse an ISO timestamp, handling timezone-aware and naive."""
    # Handle various formats
    ts = ts.strip()
    try:
        dt = datetime.fromisoformat(ts)
    except ValueError:
        # Try stripping Z and treating as UTC
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))

    # Make timezone-aware if naive
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def check_posting_limits(posts_data: dict) -> str | None:
    """Check if we've hit daily max or minimum gap. Returns reason to skip, or None if OK."""
    now = datetime.now(timezone.utc)
    today_str = now.strftime("%Y-%m-%d")

    posted_today = []
    for post in posts_data.get("posts", []):
        pa = post.get("posted_at")
        if not pa or post.get("status") != "posted":
            continue
        if today_str in pa:
            posted_today.append(parse_time(pa))

    # Check daily max
    if len(posted_today) >= MAX_POSTS_PER_DAY:
        return f"Already posted {len(posted_today)}/{MAX_POSTS_PER_DAY} today"

    # Check minimum gap since last post
    if posted_today:
        last_post_time = max(posted_today)
        hours_since = (now - last_post_time).total_seconds() / 3600
        if hours_since < MIN_GAP_HOURS:
            mins_left = int((MIN_GAP_HOURS - hours_since) * 60)
            return f"Last post was {hours_since:.1f}h ago (min gap: {MIN_GAP_HOURS}h, {mins_left}m remaining)"

    return None


CATEGORIES = [
    # Tatami categories
    "ryokan", "modern-architecture", "historic-house", "temple",
    "residential", "craft", "adaptive-reuse", "garden",
    # Museum categories
    "painting", "sculpture", "weapons", "ceramics", "jewelry",
    "textile", "prints", "furniture", "ritual", "manuscript",
    "automaton", "photography",
    "other",
]


def _get_recent_categories(posts_data: dict, n: int = 2) -> list[str]:
    """Get categories of the last N posted posts."""
    posted = _get_recent_posted(posts_data, n)
    return [p.get("category", "other") for p in posted]


def _get_recent_posted(posts_data: dict, n: int = 3) -> list[dict]:
    """Get the last N posted posts, sorted most-recent first."""
    posted = [
        p for p in posts_data.get("posts", [])
        if p.get("status") == "posted" and p.get("posted_at")
    ]
    posted.sort(key=lambda p: p.get("posted_at", ""), reverse=True)
    return posted[:n]


def _get_recent_source_handles(posts_data: dict, n: int = 3) -> list[str]:
    """Get source handles of the last N posted posts."""
    recent = _get_recent_posted(posts_data, n)
    return [p.get("source_handle", "").lstrip("@").lower() for p in recent]


def _auto_categorize(post: dict) -> str:
    """Quick keyword-based categorization from post text and metadata."""
    from config.categories import classify_museum_object, classify_tatami_post

    text = (post.get("text") or "")
    medium = (post.get("medium") or "")
    title = (post.get("title") or "")
    all_text = f"{text} {medium} {title}"

    if post.get("type") == "museum":
        return classify_museum_object(all_text)
    return classify_tatami_post(text)



# MIN_POST_SCORE, LOW_QUEUE_THRESHOLD, MIN_IMAGE_DIMENSION loaded from config at top of file


def _queue_stats(posts_data: dict) -> tuple[str, bool]:
    """Count approved and draft posts remaining. Returns (summary_str, is_low)."""
    approved = 0
    drafts = 0
    for p in posts_data.get("posts", []):
        s = p.get("status")
        if s == "approved":
            approved += 1
        elif s == "draft":
            drafts += 1
    is_low = approved <= LOW_QUEUE_THRESHOLD
    parts = [f"Queue: {approved} approved"]
    if drafts:
        parts.append(f"{drafts} drafts")
    if is_low:
        parts.append("\u26a0\ufe0f LOW — need more posts!")
    return " | ".join(parts), is_low


def find_next_post(posts_data: dict, exclude_ids: set | None = None) -> dict | None:
    """Find the best approved post to publish next.

    Called at post time by the orchestrator. Picks from ALL approved posts,
    preferring variety over recent posts:
    1. Different source handle from last 3 posts (avoid same-source runs)
    2. Different category from last 2 posts (variety)
    3. Higher score if available
    4. Falls back to random if all same category/source

    Quality gate: bookmarked posts with score < MIN_POST_SCORE get skipped.
    """
    exclude_ids = exclude_ids or set()

    ready = []
    for post in posts_data.get("posts", []):
        if post.get("status") not in ("approved",):
            continue
        if post.get("id") in exclude_ids:
            continue
        # Quality gate: skip low-score bookmarked posts
        score = post.get("score")
        if score is not None and score < MIN_POST_SCORE:
            log.info(f"Skipping post #{post.get('id')} — score {score} below threshold {MIN_POST_SCORE}")
            post["status"] = "skipped_low_quality"
            post["skip_reason"] = f"Score {score}/10 below minimum {MIN_POST_SCORE}"
            continue
        # Auto-categorize if not already tagged
        if not post.get("category"):
            post["category"] = _auto_categorize(post)
        ready.append(post)

    if not ready:
        return None

    recent_cats = _get_recent_categories(posts_data)
    recent_handles = _get_recent_source_handles(posts_data)
    log.info(f"Recent categories: {recent_cats}")
    log.info(f"Recent source handles: {recent_handles}")
    log.info(f"Ready posts: {[(p['id'], p.get('category'), p.get('source_handle','?')) for p in ready]}")

    def _pick_best(candidates: list[dict]) -> dict:
        """Pick best from candidates: highest score or random."""
        scored = [p for p in candidates if p.get("score")]
        if scored:
            scored.sort(key=lambda p: p.get("score", 0), reverse=True)
            return scored[0]
        import random
        return random.choice(candidates)

    # Filter: different source handle AND different category (best diversity)
    def _handle_of(p):
        return (p.get("source_handle") or "").lstrip("@").lower()

    fresh_handle = [p for p in ready if _handle_of(p) not in recent_handles]
    fresh_both = [p for p in fresh_handle if p.get("category") not in recent_cats]
    if fresh_both:
        return _pick_best(fresh_both)

    # At least different source handle
    if fresh_handle:
        return _pick_best(fresh_handle)

    # At least different category
    fresh_cat = [p for p in ready if p.get("category") not in recent_cats]
    if fresh_cat:
        return _pick_best(fresh_cat)

    # All same — just pick random from ready
    import random
    return random.choice(ready)


def download_post_images(post: dict) -> list[str]:
    """Download images for a post, return list of local paths."""
    paths = []

    # Check for pre-downloaded image file
    if post.get("image"):
        # Could be a relative path from project root
        img_path = BASE_DIR / post["image"]
        if img_path.exists():
            paths.append(str(img_path))
            return paths

    # Download from source URLs
    image_urls = post.get("image_urls", [])
    if not image_urls and post.get("source_url"):
        log.info("No image URLs stored, will post text-only")
        return []

    # Use source_handle for organized storage
    handle = post.get("source_handle", "unknown").lstrip("@")
    save_dir = str(BASE_DIR / "images" / handle)

    for url in image_urls:
        local_path = download_image(url, save_dir=save_dir)
        if local_path:
            paths.append(local_path)
            log.info(f"Downloaded: {Path(local_path).name}")
        else:
            log.warning(f"Failed to download: {url[:60]}...")

    return paths




def _retry_download_images(post: dict, min_dimension: int = 800) -> list[str]:
    """Re-download images with force=True to bypass cache, check quality.

    Returns list of good image paths, or empty list if all still too small.
    """
    from PIL import Image

    image_urls = post.get("image_urls", [])
    if not image_urls:
        return []

    handle = post.get("source_handle", "unknown").lstrip("@")
    save_dir = str(BASE_DIR / "images" / handle)

    good_paths = []
    for url in image_urls:
        local_path = download_image(url, save_dir=save_dir, force=True)
        if not local_path:
            continue
        try:
            with Image.open(local_path) as img:
                w, h = img.size
                if max(w, h) >= min_dimension:
                    good_paths.append(local_path)
                    log.info(f"  Re-downloaded {Path(local_path).name}: {w}x{h} OK")
                else:
                    log.info(f"  Re-downloaded {Path(local_path).name}: {w}x{h} still too small")
        except Exception as e:
            log.warning(f"  Could not check re-downloaded {local_path}: {e}")
            good_paths.append(local_path)

    return good_paths


def cross_post_to_community(post: dict, image_paths: list[str], niche: dict):
    """Cross-post to X Communities via official API.

    Supports two modes:
    - "all" list: post to every community in the list (museum)
    - "by_category" + "default": pick one community by post category (tatami)
    """
    communities = niche.get("communities")
    if not communities:
        return

    # Determine target community IDs
    community_ids = communities.get("all", [])
    if not community_ids:
        # Fallback: single community by category
        category = post.get("category", "other")
        cid = communities.get("by_category", {}).get(category) or communities.get("default")
        if cid:
            community_ids = [cid]

    if not community_ids:
        return

    text = _strip_mentions(post.get("text") or post.get("tweets", [{}])[0].get("text", ""))

    # Upload media once, reuse across communities
    media_ids = []
    if image_paths:
        for img_path in image_paths:
            mid = upload_media(img_path)
            if mid:
                media_ids.append(mid)

    posted_communities = []
    for community_id in community_ids:
        try:
            tweet_id = create_tweet(
                text=text,
                media_ids=media_ids if media_ids else None,
                community_id=community_id,
            )
            if tweet_id:
                posted_communities.append({"community_id": community_id, "tweet_id": tweet_id})
                log.info(f"  Community cross-post: {tweet_id} (community {community_id})")
            else:
                log.warning(f"  Community cross-post returned no tweet (community {community_id})")
        except Exception as e:
            log.warning(f"  Community cross-post failed (non-fatal, community {community_id}): {e}")

    if posted_communities:
        # Store first for backward compat, plus full list
        post["community_tweet_id"] = posted_communities[0]["tweet_id"]
        post["community_id"] = posted_communities[0]["community_id"]
        if len(posted_communities) > 1:
            post["community_posts"] = posted_communities


async def main():
    parser = argparse.ArgumentParser(description="Post next scheduled content")
    parser.add_argument("--niche", default="tatamispaces", help="Niche ID")
    parser.add_argument("--post-id", type=int, default=None, help="Post a specific post by ID (bypasses scheduler/limits)")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be posted without posting")
    parser.add_argument("--no-ig", action="store_true", help="Skip Instagram cross-post")
    args = parser.parse_args()

    niche_id = args.niche
    dry_run = args.dry_run
    niche = get_niche(niche_id)

    # Set X API credentials for this niche
    set_xapi_niche(niche_id)

    # Set niche for module-level load/save wrappers
    global _niche_id
    _niche_id = niche_id
    posts_file = resolve_posts_file(niche_id)

    log.info(f"Checking post queue for {niche['handle']} ({'DRY RUN' if dry_run else 'LIVE'}) — {posts_file.name}")

    posts_data = load_posts()

    # Recovery: if any post is stuck in "posting" for 30+ min, mark it failed
    for p in posts_data.get("posts", []):
        if p.get("status") == "posting":
            stuck_since = p.get("posting_started")
            if stuck_since:
                try:
                    started = parse_time(stuck_since)
                    stuck_min = (datetime.now(timezone.utc) - started).total_seconds() / 60
                    if stuck_min > 30:
                        log.warning(f"Post #{p['id']} stuck in 'posting' for {stuck_min:.0f}m — marking failed")
                        p["status"] = "failed"
                        p["fail_reason"] = "Stuck in posting state (tweet may have been sent but not recorded)"
                        save_posts(posts_data)
                except Exception:
                    pass

    # --post-id: bypass scheduler and limits, post a specific post
    if args.post_id is not None:
        post = None
        for p in posts_data.get("posts", []):
            if p.get("id") == args.post_id:
                post = p
                break
        if not post:
            log.error(f"Post #{args.post_id} not found in {posts_file.name}")
            return
        if post.get("status") == "posted":
            log.error(f"Post #{args.post_id} is already posted (tweet {post.get('tweet_id')})")
            return
        log.info(f"Manual post: #{args.post_id} (status={post.get('status')}, bypassing scheduler/limits)")
        image_paths = download_post_images(post)
        if image_paths:
            log.info(f"  Images: {len(image_paths)} downloaded")
    else:
        # Rate limiting — skip if we've hit daily max or gap is too short
        if not dry_run:
            skip_reason = check_posting_limits(posts_data)
            if skip_reason:
                log.info(f"Skipping: {skip_reason}")
                return

        # Retry loop: if a post's images all fail quality check, try the next one
        MAX_IMAGE_RETRIES = 5
        skipped_ids = set()
        post = None
        image_paths = []

        for _attempt in range(MAX_IMAGE_RETRIES):
            post = find_next_post(posts_data, exclude_ids=skipped_ids)
            save_posts(posts_data)  # persist any quality-gate skips

            if not post:
                log.info("No approved posts available to publish.")
                drafts = [p for p in posts_data.get("posts", []) if p.get("status") == "draft"]
                if drafts:
                    log.info(f"{len(drafts)} draft(s) need review. Approve them via dashboard.")
                return

            post_text = post.get("text") or (post.get("tweets") or [{}])[0].get("text", "")
            log.info(f"Trying post #{post['id']} (attempt {_attempt + 1})")
            log.info(f"  Text: {post_text[:100]}...")
            log.info(f"  Source: {post.get('source_handle', 'original')}")

            # Download images
            image_paths = download_post_images(post)
            if image_paths:
                log.info(f"  Images: {len(image_paths)} downloaded")

                # Image selection
                img_index = post.get("image_index")
                img_count = post.get("image_count")
                if img_index is not None and 0 <= img_index < len(image_paths):
                    image_paths = [image_paths[img_index]]
                    log.info(f"  Selected image #{img_index}: {Path(image_paths[0]).name}")
                elif img_count is not None and img_count < len(image_paths):
                    image_paths = image_paths[:img_count]
                    log.info(f"  Limited to first {img_count} image(s)")

                log.info(f"  Images: {len(image_paths)} ready")
            elif post.get("image_urls") or post.get("image") or post.get("allImages"):
                # Post expects images but all downloads failed (e.g. source deleted)
                log.warning(f"  Post #{post['id']} expects images but all downloads failed")
                if not dry_run:
                    post["status"] = "failed"
                    post["fail_reason"] = "All image URLs returned errors (source may be deleted)"
                    save_posts(posts_data)
                    skipped_ids.add(post["id"])
                    log.info(f"  Skipped #{post['id']}, trying next post...")
                    continue
                else:
                    print(f"WARNING: All image downloads failed — would skip in live mode")
            else:
                log.info("  No images -- will post text-only")

            # Check image quality — filter out small images
            if image_paths:
                from PIL import Image
                MIN_DIMENSION = MIN_IMAGE_DIMENSION
                good_paths = []
                bad_urls = []
                for img_path in image_paths:
                    try:
                        with Image.open(img_path) as img:
                            w, h = img.size
                            longest = max(w, h)
                            log.info(f"  Image {Path(img_path).name}: {w}x{h}")
                            if longest < MIN_DIMENSION:
                                log.warning(f"  Filtering out small image: {Path(img_path).name} ({w}x{h})")
                                bad_urls.append(img_path)
                                if dry_run:
                                    print(f"WARNING: Image {Path(img_path).name} is only {w}x{h} — would filter out in live mode")
                            else:
                                good_paths.append(img_path)
                    except Exception as e:
                        log.warning(f"  Could not check image {img_path}: {e}")
                        good_paths.append(img_path)

                if not good_paths and image_paths:
                    # Try re-downloading (cache may be stale/degraded)
                    log.info("  All images too small — trying fresh re-download...")
                    good_paths = _retry_download_images(post, MIN_DIMENSION)

                if not good_paths and image_paths:
                    log.warning(f"  ALL images failed quality check for post #{post['id']}")
                    if not dry_run:
                        post["status"] = "skipped_low_res"
                        post["skip_reason"] = f"All {len(image_paths)} images below {MIN_DIMENSION}px"
                        save_posts(posts_data)
                        skipped_ids.add(post["id"])
                        log.info(f"  Skipped #{post['id']}, trying next post...")
                        continue  # try next post
                    else:
                        print(f"WARNING: All images too small — would skip in live mode")
                else:
                    image_paths = good_paths

            # Post passed quality check (or has no images) — proceed to publish
            break
        else:
            # Exhausted all retries
            log.warning(f"All {MAX_IMAGE_RETRIES} post attempts had image quality issues")
            notify(f"{niche['handle']} post failed", f"All attempted posts had image quality issues")
            return

    # Resolve post text (works for both flat and museum formats)
    post_text = post.get("text") or (post.get("tweets") or [{}])[0].get("text", "")

    # Determine post format
    # Museum posts have type="museum" and a pre-written "tweets" array
    is_museum = post.get("type") == "museum" and post.get("tweets")
    is_museum_thread = is_museum and len(post.get("tweets", [])) > 1
    is_thread = is_museum_thread or (post.get("thread") and len(image_paths) >= 2)

    if dry_run:
        print("\n" + "=" * 60)
        if is_museum:
            n = len(post['tweets'])
            label = f"THREAD ({n} pre-written tweets)" if n > 1 else "SINGLE TWEET (museum)"
            print(f"DRY RUN -- would post {label}:")
            print("=" * 60)
            for i, tw in enumerate(post["tweets"], 1):
                print(f"\n--- Tweet {i}/{n} [{len(tw['text'])}c] ---")
                print(tw['text'])
                if tw.get("image_url"):
                    print(f"  Image: {tw['image_url'][:80]}...")
                elif tw.get("images") and post.get("allImages"):
                    for idx in tw["images"]:
                        if idx < len(post["allImages"]):
                            print(f"  Image [{idx}]: {post['allImages'][idx][:80]}...")
        elif is_thread:
            print(f"DRY RUN -- would post THREAD ({len(image_paths)} tweets):")
            print("=" * 60)
            log.info("Generating thread captions via Vision API...")
            captions = await generate_thread_captions(
                main_caption=post["text"],
                image_paths=image_paths,
                niche_id=niche_id,
            )
            for i, (cap, img) in enumerate(zip(captions, image_paths), start=1):
                print(f"\n--- Tweet {i}/{len(captions)} ---")
                print(f"Text: {cap}")
                print(f"Image: {img}")
        else:
            print("DRY RUN -- would post:")
            print("=" * 60)
            print(post["text"])
            if image_paths:
                print(f"\nWith {len(image_paths)} image(s):")
                for p in image_paths:
                    print(f"  {p}")
        print("=" * 60)
        return

    handle = niche['handle']

    # Check minimum content — reject posts that are only a credit line with no body
    if not is_thread:
        check_text = post.get("text", "")
        if is_museum and post.get("tweets"):
            check_text = post["tweets"][0].get("text", "")
        body_only = re.sub(r'📷\s*@\S+', '', check_text).strip()
        if len(body_only) < 20:
            log.error(f"Post #{post['id']} has no real body text ({len(body_only)}c without credit). Marking as failed.")
            post["status"] = "failed"
            post["fail_reason"] = f"No body text (only {len(body_only)}c without credit line)"
            save_posts(posts_data)
            notify(f"{handle} post FAILED", f"Post #{post['id']} has no body text — only credit line")
            return

    # Check character limit — Premium allows up to 4000 chars
    if not is_museum and not is_thread and len(post.get("text", "")) > 4000:
        log.error(f"Post #{post['id']} is {len(post['text'])} chars (max 4000). Marking as failed.")
        post["status"] = "failed"
        post["fail_reason"] = f"Too long: {len(post['text'])} chars"
        save_posts(posts_data)
        notify(f"{handle} post FAILED", f"Post #{post['id']} too long ({len(post['text'])} chars)")
        return

    # Dedup check — pull recent tweets from timeline and compare against this post
    log.info("Checking timeline for duplicates...")
    recent_tweets = get_own_recent_tweets(max_results=20)
    post_text_norm = post_text.strip().lower()[:80]
    for tw in recent_tweets:
        tw_text_norm = tw.get("text", "").strip().lower()[:80]
        if post_text_norm == tw_text_norm:
            existing_id = tw["id"]
            log.warning(f"DUPLICATE DETECTED — post #{post['id']} already live as tweet {existing_id}")
            post["status"] = "posted"
            post["tweet_id"] = existing_id
            post["posted_at"] = tw.get("created_at", datetime.now(timezone.utc).isoformat())
            post["dedup_recovered"] = True
            save_posts(posts_data)
            notify(f"{handle} dedup", f"Post #{post['id']} was already live (tweet {existing_id}), marked as posted")
            return

    # Post via official API
    log.info("Posting via official API...")

    # Mark as "posting" BEFORE sending — prevents duplicate posts if save fails after tweeting
    post["status"] = "posting"
    post["posting_started"] = datetime.now(timezone.utc).isoformat()
    save_posts(posts_data)

    if is_museum:
        await _publish_museum(post, posts_data, niche, handle)
    elif is_thread:
        await _publish_tatami_thread(post, posts_data, niche, handle, niche_id, image_paths)
    else:
        await _publish_single(post, posts_data, niche, handle, image_paths)


def _mark_failed(post, posts_data, handle, reason):
    """Mark a post as failed and notify."""
    post["status"] = "failed"
    post["fail_reason"] = reason
    save_posts(posts_data)
    log.error(f"Failed: {reason}")
    notify(f"{handle} post FAILED", f"Post #{post['id']} {reason}")


def cross_post_to_bluesky(post: dict, image_paths: list[str], niche: dict):
    """Cross-post to Bluesky. Non-fatal — logs warnings on failure.

    Handles 3 post formats:
    - Flat (tatami/cosmic): single post or auto-split if >300 graphemes
    - Museum (tweets array): map to Bluesky thread
    - Thread (tatami threads): map thread_captions to Bluesky thread
    """
    if not niche.get("bluesky_env"):
        return

    try:
        set_bsky_niche(_niche_id)

        is_museum = post.get("type") == "museum" and post.get("tweets")
        has_thread_captions = post.get("thread_captions") and len(post.get("thread_captions", [])) > 1

        if is_museum and len(post.get("tweets", [])) > 1:
            # Museum thread — map tweets array to Bluesky thread
            thread_posts = []
            for i, tw in enumerate(post["tweets"]):
                tw_images = []
                if tw.get("image_url"):
                    tw_images = [tw["image_url"]]
                elif tw.get("images") and post.get("allImages"):
                    tw_images = [post["allImages"][idx] for idx in tw["images"] if idx < len(post["allImages"])]

                # Resolve to local paths
                local_paths = []
                for img_url in tw_images:
                    local_path = download_image(img_url, save_dir=str(IMAGES_DIR / "museum"))
                    if local_path:
                        local_paths.append(local_path)

                thread_posts.append({
                    "text": _strip_mentions(tw["text"]),
                    "image_paths": local_paths,
                    "alt_texts": [],
                })

            uris = bsky_post_thread(thread_posts)
            if uris:
                post["bluesky_post_uri"] = uris[0]
                post["bluesky_thread_uris"] = uris
                log.info(f"  Bluesky cross-post: {len(uris)}-post thread")

        elif has_thread_captions:
            # Tatami thread — use saved captions
            thread_posts = []
            captions = post["thread_captions"]
            for i, cap in enumerate(captions):
                img = [image_paths[i]] if i < len(image_paths) else []
                thread_posts.append({
                    "text": _strip_mentions(cap),
                    "image_paths": img,
                    "alt_texts": [],
                })

            uris = bsky_post_thread(thread_posts)
            if uris:
                post["bluesky_post_uri"] = uris[0]
                post["bluesky_thread_uris"] = uris
                log.info(f"  Bluesky cross-post: {len(uris)}-post thread")

        else:
            # Flat post (tatami single, museum single, cosmic)
            text = _strip_mentions(post.get("text") or "")
            if is_museum and post.get("tweets"):
                text = _strip_mentions(post["tweets"][0].get("text", ""))

            uri = bsky_create_post(text=text, image_paths=image_paths)
            if uri:
                post["bluesky_post_uri"] = uri
                log.info(f"  Bluesky cross-post: single post")

    except Exception as e:
        log.warning(f"  Bluesky cross-post failed (non-fatal): {e}")


def _mark_posted(post, posts_data, handle, tweet_ids, fmt_label, niche, community_images):
    """Mark a post as posted, notify, and cross-post to communities."""
    post["status"] = "posted"
    post["posted_at"] = datetime.now(timezone.utc).isoformat()
    post["tweet_id"] = tweet_ids[0]
    if len(tweet_ids) > 1:
        post["thread_tweet_ids"] = tweet_ids
    save_posts(posts_data)

    post_url = f"https://x.com/{handle.lstrip('@')}/status/{tweet_ids[0]}"
    log.info(f"Posted ({fmt_label}): {post_url}")
    queue_info, queue_low = _queue_stats(posts_data)
    notify(f"{handle} posted", f"Post #{post['id']} — {fmt_label}\n{queue_info}", priority="high" if queue_low else "default")

    cross_post_to_community(post, community_images, niche)
    cross_post_to_bluesky(post, community_images, niche)
    save_posts(posts_data)


async def _publish_museum(post: dict, posts_data: dict, niche: dict, handle: str):
    """Publish a museum-format post (pre-written tweets with per-tweet images)."""
    n_tweets = len(post['tweets'])
    log.info(f"Posting museum {'thread' if n_tweets > 1 else 'single'} ({n_tweets} tweet{'s' if n_tweets > 1 else ''}) via X API v2...")

    # Download per-tweet images
    thread_data = []
    for i, tw in enumerate(post["tweets"]):
        image_urls = []
        if tw.get("image_url"):
            image_urls = [tw["image_url"]]
        elif tw.get("images") and post.get("allImages"):
            image_urls = [post["allImages"][idx] for idx in tw["images"] if idx < len(post["allImages"])]

        local_paths = []
        for img_url in image_urls:
            local_path = download_image(img_url, save_dir=str(IMAGES_DIR / "museum"))
            if local_path:
                local_paths.append(local_path)
            else:
                log.error(f"  Tweet {i+1}: failed to download image {img_url[:80]}")
                _mark_failed(post, posts_data, handle, f"Image download failed for tweet {i+1}")
                return

        thread_data.append({"text": _strip_mentions(tw["text"]), "image_paths": local_paths})

    tweet_ids = post_thread(tweets=thread_data, delay_seconds=(3, 8))

    if not tweet_ids:
        _mark_failed(post, posts_data, handle, "Thread posting returned no tweet IDs")
        return

    expected = len(thread_data)
    is_partial = len(tweet_ids) < expected

    if is_partial:
        post["status"] = "partial_thread"
        post["fail_reason"] = f"Only {len(tweet_ids)}/{expected} tweets posted"
        post["posted_at"] = datetime.now(timezone.utc).isoformat()
        post["tweet_id"] = tweet_ids[0]
        post["thread_tweet_ids"] = tweet_ids
        save_posts(posts_data)
        log.error(f"PARTIAL THREAD: {len(tweet_ids)}/{expected} tweets posted for post #{post['id']}")
        queue_info, queue_low = _queue_stats(posts_data)
        notify(f"{handle} PARTIAL", f"Post #{post['id']} — {len(tweet_ids)}/{expected} tweets\n{queue_info}", priority="high")
    else:
        first_images = thread_data[0].get("image_paths", []) if thread_data else []
        fmt = f"{len(tweet_ids)}-tweet thread" if len(tweet_ids) > 1 else "single tweet"
        _mark_posted(post, posts_data, handle, tweet_ids, fmt, niche, first_images)


async def _publish_tatami_thread(post: dict, posts_data: dict, niche: dict, handle: str, niche_id: str, image_paths: list):
    """Publish a tatami-format thread (auto-generate captions from images)."""
    community_id = niche.get("community_id")
    log.info(f"Posting as thread ({len(image_paths)} images)...")

    captions = await generate_thread_captions(
        main_caption=post["text"],
        image_paths=image_paths,
        niche_id=niche_id,
    )

    thread_data = [
        {"text": _strip_mentions(cap), "image_paths": [img]}
        for cap, img in zip(captions, image_paths)
    ]

    tweet_ids = post_thread(tweets=thread_data, delay_seconds=(3, 8), community_id=community_id)

    if not tweet_ids:
        _mark_failed(post, posts_data, handle, "Thread posting returned no tweet IDs")
        return

    post["thread_captions"] = captions
    first_image = [image_paths[0]] if image_paths else []
    _mark_posted(post, posts_data, handle, tweet_ids, f"{len(tweet_ids)}-tweet thread", niche, first_image)


async def _publish_single(post: dict, posts_data: dict, niche: dict, handle: str, image_paths: list):
    """Publish a single tweet."""
    media_ids = []
    for img_path in image_paths:
        mid = upload_media(img_path)
        if mid:
            media_ids.append(mid)
            log.info(f"  Uploaded media: {Path(img_path).name}")
        else:
            log.warning(f"  Failed to upload: {img_path}")

    tweet_id = create_tweet(
        text=_strip_mentions(post["text"]),
        media_ids=media_ids if media_ids else None,
        quote_tweet_id=post.get("quote_tweet_id"),
    )

    if not tweet_id:
        _mark_failed(post, posts_data, handle, "create_tweet returned no tweet ID")
        return

    _mark_posted(post, posts_data, handle, [tweet_id], "single tweet", niche, image_paths)


if __name__ == "__main__":
    # Parse niche early for niche-specific lock
    _pre = argparse.ArgumentParser(add_help=False)
    _pre.add_argument("--niche", default="tatamispaces")
    _pre_args, _ = _pre.parse_known_args()
    lock_name = f"post_{_pre_args.niche}"

    if not acquire_process_lock(lock_name):
        log.info("Another post.py is already running, exiting")
        sys.exit(0)
    try:
        asyncio.run(main())
    finally:
        release_process_lock(lock_name)
