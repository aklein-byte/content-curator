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
import logging
import argparse
from pathlib import Path
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).parent))

from dotenv import load_dotenv
load_dotenv()

from tools.xkit import login, XPost
from agents.engager import evaluate_post, draft_original_post
from config.niches import get_niche

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("bookmarks")

BASE_DIR = Path(__file__).parent
POSTS_FILE = Path(os.environ.get("POSTS_FILE", str(BASE_DIR / "posts.json")))


def load_posts() -> dict:
    if POSTS_FILE.exists():
        return json.loads(POSTS_FILE.read_text())
    return {"posts": []}


def save_posts(data: dict):
    POSTS_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False, default=str))


def next_post_id(posts_data: dict) -> int:
    existing_ids = [p.get("id", 0) for p in posts_data.get("posts", [])]
    return max(existing_ids, default=0) + 1


def already_in_queue(posts_data: dict, post_id: str) -> bool:
    """Check if a post ID or source URL containing it is already queued."""
    for p in posts_data.get("posts", []):
        src = p.get("source_url", "")
        if post_id in src:
            return True
    return False


def parse_bookmark(tweet) -> XPost:
    """Convert a twikit tweet object to our XPost dataclass."""
    images = []
    if hasattr(tweet, 'media') and tweet.media:
        for media in tweet.media:
            if hasattr(media, 'media_url_https'):
                images.append(media.media_url_https)
            elif isinstance(media, dict) and 'media_url_https' in media:
                images.append(media['media_url_https'])

    return XPost(
        post_id=tweet.id,
        author_handle=tweet.user.screen_name if tweet.user else "",
        author_name=tweet.user.name if tweet.user else "",
        author_id=tweet.user.id if tweet.user else "",
        text=tweet.text or "",
        image_urls=images,
        likes=tweet.favorite_count or 0,
        reposts=tweet.retweet_count or 0,
        replies=tweet.reply_count or 0,
        views=tweet.view_count or 0,
        language=getattr(tweet, 'lang', None),
        created_at=str(tweet.created_at) if hasattr(tweet, 'created_at') else None,
    )


async def main():
    parser = argparse.ArgumentParser(description="Turn bookmarks into post drafts")
    parser.add_argument("--niche", default="tatamispaces", help="Niche ID")
    parser.add_argument("--max-drafts", type=int, default=10, help="Max new drafts to create")
    parser.add_argument("--min-score", type=int, default=6, help="Minimum relevance score (1-10)")
    args = parser.parse_args()

    niche_id = args.niche
    niche = get_niche(niche_id)

    log.info(f"Fetching bookmarks for {niche['handle']}")

    # Login
    client = await login(niche_id)
    log.info("Logged in")

    # Fetch bookmarks
    bookmarks = await client.get_bookmarks(count=40)
    bookmark_posts = [parse_bookmark(t) for t in bookmarks]
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

        score = evaluation["relevance_score"]
        if score < args.min_score:
            log.info(f"  Skip @{post.author_handle} — score {score}/10: {evaluation['reason'][:50]}")
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

        source_url = f"https://x.com/{post.author_handle}/status/{post.post_id}"

        new_post = {
            "id": next_post_id(posts_data),
            "type": "repost-with-credit",
            "text": caption_text,
            "image": None,
            "image_urls": post.image_urls,
            "source_url": source_url,
            "source_handle": f"@{post.author_handle}",
            "status": "draft",
            "notes": f"From bookmarks. Score {score}/10. {post.likes} likes. {evaluation['reason'][:80]}",
        }

        posts_data["posts"].append(new_post)
        drafts_created += 1
        log.info(f"  Draft #{new_post['id']}: {caption_text[:80]}...")

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

    # Notify
    try:
        os.system(
            f'terminal-notifier -title "@tatamispaces bookmarks" '
            f'-message "{drafts_created} new drafts from bookmarks" '
            f'-sound default -group content-curator 2>/dev/null'
        )
    except Exception:
        pass


if __name__ == "__main__":
    asyncio.run(main())
