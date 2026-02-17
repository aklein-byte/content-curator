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
from tools.common import load_json, save_json, notify, acquire_lock, release_lock, setup_logging, load_config
from agents.writer import generate_thread_captions
from config.niches import get_niche

log = setup_logging("post")

BASE_DIR = Path(__file__).parent
IMAGES_DIR = BASE_DIR / "data" / "images"

# Posting limits — prevent rapid-fire after downtime
MAX_POSTS_PER_DAY = 3
MIN_GAP_HOURS = 2

# Resolved once by main(), used by load_posts/save_posts
_resolved_posts_file: Path | None = None


def _resolve_posts_file(niche_id: str) -> Path:
    """Resolve the posts file path for a niche. Called once by main()."""
    env_file = os.environ.get("POSTS_FILE")
    if env_file:
        return Path(env_file)
    niche = get_niche(niche_id)
    filename = niche.get("posts_file", "posts.json")
    return BASE_DIR / filename


def load_posts() -> dict:
    return load_json(_resolved_posts_file, default={"posts": []})


def save_posts(data: dict):
    save_json(_resolved_posts_file, data, lock=True)


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
    posted = [
        p for p in posts_data.get("posts", [])
        if p.get("status") == "posted" and p.get("posted_at")
    ]
    posted.sort(key=lambda p: p.get("posted_at", ""), reverse=True)
    return [p.get("category", "other") for p in posted[:n]]


def _auto_categorize(post: dict) -> str:
    """Quick keyword-based categorization from post text and metadata."""
    text = (post.get("text") or "").lower()
    medium = (post.get("medium") or "").lower()
    title = (post.get("title") or "").lower()
    all_text = f"{text} {medium} {title}"

    # Museum categories (check first — museum posts have type="museum")
    if post.get("type") == "museum":
        if any(w in all_text for w in ["painting", "oil on canvas", "watercolor", "fresco", "painted"]):
            return "painting"
        if any(w in all_text for w in ["sculpture", "statue", "bust", "relief", "carved figure"]):
            return "sculpture"
        if any(w in all_text for w in ["sword", "armor", "dagger", "shield", "weapon", "helmet"]):
            return "weapons"
        if any(w in all_text for w in ["ceramic", "pottery", "porcelain", "vase", "stoneware", "faience"]):
            return "ceramics"
        if any(w in all_text for w in ["jewelry", "ring", "necklace", "brooch", "crown", "tiara", "cameo"]):
            return "jewelry"
        if any(w in all_text for w in ["textile", "silk", "tapestry", "fabric", "embroidery", "kimono", "costume"]):
            return "textile"
        if any(w in all_text for w in ["print", "woodcut", "etching", "lithograph", "woodblock"]):
            return "prints"
        if any(w in all_text for w in ["furniture", "chair", "table", "cabinet", "desk"]):
            return "furniture"
        if any(w in all_text for w in ["mask", "ritual", "ceremony", "reliquary", "votive", "altar"]):
            return "ritual"
        if any(w in all_text for w in ["manuscript", "illuminat", "codex", "calligraph", "scroll"]):
            return "manuscript"
        if any(w in all_text for w in ["automaton", "clockwork", "mechanical", "clock"]):
            return "automaton"
        if any(w in all_text for w in ["photograph", "daguerreotype", "albumen"]):
            return "photography"
        return "other"

    # Tatami categories
    if any(w in text for w in ["ryokan", "onsen", "rotenburo", "hot spring", "bath"]):
        return "ryokan"
    if any(w in text for w in ["temple", "shrine", "jinja", "tera", "karesansui"]):
        return "temple"
    if any(w in text for w in ["kominka", "machiya", "taisho", "meiji", "edo-period", "former residence", "preserved"]):
        return "historic-house"
    if any(w in text for w in ["architect", "concrete", "steel", "shell", "parabolic", "brutalist", "modernist"]):
        return "modern-architecture"
    if any(w in text for w in ["tatami mat", "apartment", "1ldk", "2ldk", "small space"]):
        return "residential"
    if any(w in text for w in ["kumiko", "woodwork", "lacquer", "ceramic", "craft", "joinery"]):
        return "craft"
    if any(w in text for w in ["sauna", "converted", "repurposed", "adaptive", "pop-up"]):
        return "adaptive-reuse"
    if any(w in text for w in ["garden", "engawa", "landscape", "moss", "stone path"]):
        return "garden"
    return "other"


MIN_POST_SCORE = 7  # Skip bookmarked posts below this score


def find_next_post(posts_data: dict) -> dict | None:
    """Find the best approved post to publish next.

    Picks from ready posts, preferring:
    1. Different category from last 2 posts (variety)
    2. Higher score if available
    3. Falls back to random if all same category

    Quality gate: bookmarked posts with score < MIN_POST_SCORE get skipped.
    """
    now = datetime.now(timezone.utc)

    ready = []
    for post in posts_data.get("posts", []):
        if post.get("status") not in ("approved",):
            continue
        scheduled = post.get("scheduled_for")
        if not scheduled:
            continue
        try:
            scheduled_dt = parse_time(scheduled)
            if scheduled_dt <= now:
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
        except Exception as e:
            log.warning(f"Bad scheduled_for on post #{post.get('id')}: {e}")

    if not ready:
        return None

    recent_cats = _get_recent_categories(posts_data)
    log.info(f"Recent categories: {recent_cats}")
    log.info(f"Ready posts: {[(p['id'], p.get('category')) for p in ready]}")

    # Prefer posts NOT in recent categories
    diverse = [p for p in ready if p.get("category") not in recent_cats]
    if diverse:
        # Pick highest score, or random if no scores
        scored = [p for p in diverse if p.get("score")]
        if scored:
            scored.sort(key=lambda p: p.get("score", 0), reverse=True)
            return scored[0]
        import random
        return random.choice(diverse)

    # All same category — just pick random from ready
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

    text = post.get("text") or post.get("tweets", [{}])[0].get("text", "")

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

    # Resolve posts file for this niche (must happen before any load_posts/save_posts calls)
    global _resolved_posts_file
    _resolved_posts_file = _resolve_posts_file(niche_id)

    log.info(f"Checking post queue for {niche['handle']} ({'DRY RUN' if dry_run else 'LIVE'}) — {_resolved_posts_file.name}")

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
            log.error(f"Post #{args.post_id} not found in {_resolved_posts_file.name}")
            return
        if post.get("status") == "posted":
            log.error(f"Post #{args.post_id} is already posted (tweet {post.get('tweet_id')})")
            return
        log.info(f"Manual post: #{args.post_id} (status={post.get('status')}, bypassing scheduler/limits)")
    else:
        # Rate limiting — skip if we've hit daily max or gap is too short
        if not dry_run:
            skip_reason = check_posting_limits(posts_data)
            if skip_reason:
                log.info(f"Skipping: {skip_reason}")
                return

        post = find_next_post(posts_data)
        save_posts(posts_data)  # persist any quality-gate skips

        if not post:
            log.info("No posts ready to publish. Queue is empty or nothing scheduled yet.")

            # Show upcoming
            approved = [
                p for p in posts_data.get("posts", [])
                if p.get("status") == "approved" and p.get("scheduled_for")
            ]
            if approved:
                log.info("Upcoming approved posts:")
                for p in approved:
                    log.info(f"  #{p['id']} — scheduled {p['scheduled_for']}")
            else:
                drafts = [p for p in posts_data.get("posts", []) if p.get("status") == "draft"]
                if drafts:
                    log.info(f"{len(drafts)} draft(s) need review. Edit posts.json to approve them.")
            return

    post_text = post.get("text") or (post.get("tweets") or [{}])[0].get("text", "")
    log.info(f"Found post #{post['id']} ready to publish")
    log.info(f"  Text: {post_text[:100]}...")
    log.info(f"  Source: {post.get('source_handle', 'original')}")

    # Download images
    image_paths = download_post_images(post)
    if image_paths:
        log.info(f"  Images: {len(image_paths)} downloaded")

        # Image selection: use image_index for specific image, image_count for limit
        img_index = post.get("image_index")
        img_count = post.get("image_count")
        if img_index is not None and 0 <= img_index < len(image_paths):
            image_paths = [image_paths[img_index]]
            log.info(f"  Selected image #{img_index}: {Path(image_paths[0]).name}")
        elif img_count is not None and img_count < len(image_paths):
            image_paths = image_paths[:img_count]
            log.info(f"  Limited to first {img_count} image(s)")

        log.info(f"  Images: {len(image_paths)} ready")
    else:
        log.info("  No images -- will post text-only")

    # Check image quality — filter out small images, skip post only if ALL fail
    if image_paths:
        from PIL import Image
        MIN_DIMENSION = 800  # minimum pixels on longest side
        good_paths = []
        for img_path in image_paths:
            try:
                with Image.open(img_path) as img:
                    w, h = img.size
                    longest = max(w, h)
                    log.info(f"  Image {Path(img_path).name}: {w}x{h}")
                    if longest < MIN_DIMENSION:
                        log.warning(f"  Filtering out small image: {Path(img_path).name} ({w}x{h})")
                        if dry_run:
                            print(f"WARNING: Image {Path(img_path).name} is only {w}x{h} — would filter out in live mode")
                    else:
                        good_paths.append(img_path)
            except Exception as e:
                log.warning(f"  Could not check image {img_path}: {e}")
                good_paths.append(img_path)  # keep if we can't check

        if not good_paths and image_paths:
            log.warning(f"  ALL images failed quality check")
            if not dry_run:
                post["status"] = "skipped_low_res"
                post["skip_reason"] = f"All {len(image_paths)} images below {MIN_DIMENSION}px"
                save_posts(posts_data)
                notify(f"{niche['handle']} post skipped", f"Post #{post['id']} all images too small")
                return
            else:
                print(f"WARNING: All images too small — would skip in live mode")
        else:
            image_paths = good_paths

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
        # Museum format: pre-written tweets with per-tweet images, posted via X API v2
        n_tweets = len(post['tweets'])
        log.info(f"Posting museum {'thread' if n_tweets > 1 else 'single'} ({n_tweets} tweet{'s' if n_tweets > 1 else ''}) via X API v2...")

        # Download per-tweet images
        thread_data = []
        for i, tw in enumerate(post["tweets"]):
            # Resolve image URLs: direct image_url OR indices into allImages
            image_urls = []
            if tw.get("image_url"):
                image_urls = [tw["image_url"]]
            elif tw.get("images") and post.get("allImages"):
                image_urls = [post["allImages"][idx] for idx in tw["images"] if idx < len(post["allImages"])]

            local_paths = []
            for img_url in image_urls:
                save_dir = str(IMAGES_DIR / "museum")
                local_path = download_image(img_url, save_dir=save_dir)
                if local_path:
                    local_paths.append(local_path)
                else:
                    log.error(f"  Tweet {i+1}: failed to download image {img_url[:80]}")
                    post["status"] = "failed"
                    post["fail_reason"] = f"Image download failed for tweet {i+1}"
                    save_posts(posts_data)
                    notify(f"{handle} post FAILED", f"Post #{post['id']} image download failed")
                    return

            thread_data.append({
                "text": tw["text"],
                "image_paths": local_paths,
            })

        tweet_ids = post_thread(
            tweets=thread_data,
            delay_seconds=(120, 300),
        )

        if tweet_ids:
            post["status"] = "posted"
            post["posted_at"] = datetime.now(timezone.utc).isoformat()
            post["tweet_id"] = tweet_ids[0]
            post["thread_tweet_ids"] = tweet_ids
            save_posts(posts_data)

            post_url = f"https://x.com/{handle.lstrip('@')}/status/{tweet_ids[0]}"
            fmt_label = f"{len(tweet_ids)}-tweet thread" if len(tweet_ids) > 1 else "single tweet"
            log.info(f"Posted ({fmt_label}): {post_url}")
            notify(f"{handle} posted", f"Post #{post['id']} — {fmt_label}")

            first_images = thread_data[0].get("image_paths", []) if thread_data else []
            cross_post_to_community(post, first_images, niche)
            save_posts(posts_data)
        else:
            post["status"] = "failed"
            post["fail_reason"] = "Thread posting returned no tweet IDs"
            save_posts(posts_data)
            log.error("Failed to post thread.")
            notify(f"{handle} post FAILED", f"Post #{post['id']} thread failed")

    elif is_thread:
        # Tatami format: auto-generate captions from images, post via X API v2
        community_id = niche.get("community_id")
        log.info(f"Posting as thread ({len(image_paths)} images)...")
        captions = await generate_thread_captions(
            main_caption=post["text"],
            image_paths=image_paths,
            niche_id=niche_id,
        )

        thread_data = [
            {"text": cap, "image_paths": [img]}
            for cap, img in zip(captions, image_paths)
        ]

        tweet_ids = post_thread(
            tweets=thread_data,
            delay_seconds=(120, 300),
            community_id=community_id,
        )

        if tweet_ids:
            post["status"] = "posted"
            post["posted_at"] = datetime.now(timezone.utc).isoformat()
            post["tweet_id"] = tweet_ids[0]
            post["thread_tweet_ids"] = tweet_ids
            post["thread_captions"] = captions
            save_posts(posts_data)

            post_url = f"https://x.com/{handle.lstrip('@')}/status/{tweet_ids[0]}"
            log.info(f"Thread posted ({len(tweet_ids)} tweets): {post_url}")
            notify(f"{handle} thread posted", f"Post #{post['id']} — {len(tweet_ids)} tweet thread")

            first_image = [image_paths[0]] if image_paths else []
            cross_post_to_community(post, first_image, niche)
            save_posts(posts_data)
        else:
            post["status"] = "failed"
            post["fail_reason"] = "Thread posting returned no tweet IDs"
            save_posts(posts_data)
            log.error("Failed to post thread.")
            notify(f"{handle} post FAILED", f"Post #{post['id']} thread failed")

    else:
        # Single tweet via official API v2
        media_ids = []
        if image_paths:
            for img_path in image_paths:
                mid = upload_media(img_path)
                if mid:
                    media_ids.append(mid)
                    log.info(f"  Uploaded media: {Path(img_path).name}")
                else:
                    log.warning(f"  Failed to upload: {img_path}")

        quote_tweet_id = post.get("quote_tweet_id")

        tweet_id = create_tweet(
            text=post["text"],
            media_ids=media_ids if media_ids else None,
            quote_tweet_id=quote_tweet_id,
        )

        if tweet_id:
            post["status"] = "posted"
            post["posted_at"] = datetime.now(timezone.utc).isoformat()
            post["tweet_id"] = tweet_id
            save_posts(posts_data)

            post_url = f"https://x.com/{handle.lstrip('@')}/status/{tweet_id}"
            log.info(f"Posted successfully: {post_url}")
            notify(f"{handle} posted", f"Post #{post['id']} is live")

            cross_post_to_community(post, image_paths, niche)
            save_posts(posts_data)
        else:
            post["status"] = "failed"
            post["fail_reason"] = "create_tweet returned no tweet ID"
            save_posts(posts_data)
            log.error("Failed to post.")
            notify(f"{handle} post FAILED", f"Post #{post['id']} failed to publish")


if __name__ == "__main__":
    lock_fd = acquire_lock(BASE_DIR / ".post.lock")
    if not lock_fd:
        log.info("Another post.py is already running, exiting")
        sys.exit(0)
    try:
        asyncio.run(main())
    finally:
        release_lock(lock_fd)
