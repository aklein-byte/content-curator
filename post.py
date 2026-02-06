"""
Posting script for @tatamispaces.
Reads posts.json, finds the next approved+scheduled post whose time has passed,
downloads the image, posts via twikit, and marks it as posted.

Usage: python post.py [--niche tatamispaces] [--dry-run]
"""

import sys
import os
import json
import asyncio
import logging
import argparse
from pathlib import Path
from datetime import datetime, timezone

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent))

from dotenv import load_dotenv
load_dotenv()

from tools.xkit import login, post_tweet, download_image
from config.niches import get_niche

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("post")

BASE_DIR = Path(__file__).parent
POSTS_FILE = Path(os.environ.get("POSTS_FILE", str(BASE_DIR / "posts.json")))
IMAGES_DIR = BASE_DIR / "data" / "images"


def load_posts() -> dict:
    if POSTS_FILE.exists():
        return json.loads(POSTS_FILE.read_text())
    return {"posts": []}


def save_posts(data: dict):
    POSTS_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False, default=str))


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


def find_next_post(posts_data: dict) -> dict | None:
    """Find the next approved post whose scheduled_for time has passed."""
    now = datetime.now(timezone.utc)

    ready = []
    for post in posts_data.get("posts", []):
        if post.get("status") != "approved":
            continue
        scheduled = post.get("scheduled_for")
        if not scheduled:
            continue
        try:
            scheduled_dt = parse_time(scheduled)
            if scheduled_dt <= now:
                ready.append((scheduled_dt, post))
        except Exception as e:
            log.warning(f"Bad scheduled_for on post #{post.get('id')}: {e}")

    if not ready:
        return None

    # Return the earliest-scheduled ready post
    ready.sort(key=lambda x: x[0])
    return ready[0][1]


async def download_post_images(post: dict) -> list[str]:
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
        local_path = await download_image(url, save_dir=save_dir)
        if local_path:
            paths.append(local_path)
            log.info(f"Downloaded: {Path(local_path).name}")
        else:
            log.warning(f"Failed to download: {url[:60]}...")

    return paths


def notify(title: str, message: str):
    try:
        os.system(
            f'terminal-notifier -title "{title}" -message "{message}" '
            f'-sound default -group content-curator 2>/dev/null'
        )
    except Exception:
        pass


async def main():
    parser = argparse.ArgumentParser(description="Post next scheduled content")
    parser.add_argument("--niche", default="tatamispaces", help="Niche ID")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be posted without posting")
    args = parser.parse_args()

    niche_id = args.niche
    dry_run = args.dry_run
    niche = get_niche(niche_id)

    log.info(f"Checking post queue for {niche['handle']} ({'DRY RUN' if dry_run else 'LIVE'})")

    posts_data = load_posts()
    post = find_next_post(posts_data)

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

    log.info(f"Found post #{post['id']} ready to publish")
    log.info(f"  Text: {post['text'][:100]}...")
    log.info(f"  Source: {post.get('source_handle', 'original')}")

    # Download images
    image_paths = await download_post_images(post)
    if image_paths:
        log.info(f"  Images: {len(image_paths)} ready")
    else:
        log.info("  No images -- will post text-only")

    if dry_run:
        print("\n" + "=" * 60)
        print("DRY RUN -- would post:")
        print("=" * 60)
        print(post["text"])
        if image_paths:
            print(f"\nWith {len(image_paths)} image(s):")
            for p in image_paths:
                print(f"  {p}")
        print("=" * 60)
        return

    # Login and post
    client = await login(niche_id)
    log.info("Logged in, posting now...")

    tweet_id = await post_tweet(
        client=client,
        text=post["text"],
        image_paths=image_paths if image_paths else None,
    )

    if tweet_id:
        # Mark as posted
        post["status"] = "posted"
        post["posted_at"] = datetime.now(timezone.utc).isoformat()
        post["tweet_id"] = tweet_id
        save_posts(posts_data)

        post_url = f"https://x.com/{niche['handle'].lstrip('@')}/status/{tweet_id}"
        log.info(f"Posted successfully: {post_url}")
        notify("@tatamispaces posted", f"Post #{post['id']} is live")
    else:
        log.error("Failed to post. Check logs above for details.")
        notify("@tatamispaces post FAILED", f"Post #{post['id']} failed to publish")


if __name__ == "__main__":
    asyncio.run(main())
