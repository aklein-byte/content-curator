"""
Engagement script for @tatamispaces.
Searches JP architecture posts, auto-likes high-relevance content,
drafts replies for review, follows a few relevant accounts.

Usage: python engage.py [--niche tatamispaces] [--dry-run]
"""

import sys
import os
import json
import asyncio
import random
import logging
import argparse
from pathlib import Path
from datetime import datetime, timezone

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent))

from dotenv import load_dotenv
load_dotenv()

from tools.xkit import login, search_posts, like_post, follow_user, reply_to_post, XPost
from agents.engager import evaluate_post, draft_reply
from config.niches import get_niche

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("engage")

BASE_DIR = Path(__file__).parent
ENGAGEMENT_LOG = Path(os.environ.get("ENGAGEMENT_LOG", str(BASE_DIR / "engagement-log.json")))
ENGAGEMENT_DRAFTS = Path(os.environ.get("ENGAGEMENT_DRAFTS", str(BASE_DIR / "engagement-drafts.json")))

# Limits
MAX_LIKES_PER_RUN = 15
MAX_REPLIES_PER_RUN = 5
MAX_FOLLOWS_PER_RUN = 3
POSTS_PER_QUERY = 15

# Delay range between actions (seconds)
DELAY_MIN = 30
DELAY_MAX = 120


def load_json(path: Path) -> list | dict:
    if path.exists():
        return json.loads(path.read_text())
    return []


def save_json(path: Path, data):
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False, default=str))


def already_engaged(log_entries: list, post_id: str) -> bool:
    """Check if we already liked/replied to this post."""
    return any(e["post_id"] == post_id for e in log_entries)


async def random_delay(label: str = ""):
    """Sleep a random duration to look human."""
    wait = random.uniform(DELAY_MIN, DELAY_MAX)
    if label:
        log.info(f"Waiting {wait:.0f}s before {label}...")
    await asyncio.sleep(wait)


def notify(title: str, message: str):
    """macOS notification, fails silently if terminal-notifier not installed."""
    try:
        os.system(
            f'terminal-notifier -title "{title}" -message "{message}" '
            f'-sound default -group content-curator 2>/dev/null'
        )
    except Exception:
        pass


async def main():
    parser = argparse.ArgumentParser(description="Engage with JP architecture posts")
    parser.add_argument("--niche", default="tatamispaces", help="Niche ID")
    parser.add_argument("--dry-run", action="store_true", help="Evaluate only, no actions")
    args = parser.parse_args()

    niche_id = args.niche
    dry_run = args.dry_run
    niche = get_niche(niche_id)
    engagement_cfg = niche.get("engagement", {})
    queries = engagement_cfg.get("search_queries", [])

    if not queries:
        log.error(f"No search queries configured for niche '{niche_id}'")
        return

    log.info(f"Starting engagement for {niche['handle']} ({'DRY RUN' if dry_run else 'LIVE'})")

    # Login
    client = await login(niche_id)
    log.info("Logged in successfully")

    # Load existing logs
    engagement_log = load_json(ENGAGEMENT_LOG)
    if not isinstance(engagement_log, list):
        engagement_log = []

    # Gather posts from all queries
    all_posts: list[XPost] = []
    seen_ids = set()

    # Shuffle queries so we don't always hit the same ones first
    shuffled_queries = random.sample(queries, min(len(queries), 4))

    for query in shuffled_queries:
        log.info(f"Searching: {query[:60]}...")
        posts = await search_posts(client, query, count=POSTS_PER_QUERY, product="Top")
        for p in posts:
            if p.post_id not in seen_ids:
                seen_ids.add(p.post_id)
                all_posts.append(p)
        await random_delay("next search")

    log.info(f"Found {len(all_posts)} unique posts across {len(shuffled_queries)} queries")

    # Evaluate all posts with Claude
    scored_posts = []
    for post in all_posts:
        if already_engaged(engagement_log, post.post_id):
            continue

        evaluation = await evaluate_post(
            post_text=post.text,
            author=post.author_handle,
            niche_id=niche_id,
            image_count=len(post.image_urls),
            likes=post.likes,
            reposts=post.reposts,
        )
        scored_posts.append((post, evaluation))
        log.info(
            f"  @{post.author_handle} — score {evaluation['relevance_score']}/10 "
            f"({evaluation['reason'][:50]})"
        )

    # Sort by relevance score descending
    scored_posts.sort(key=lambda x: x[1]["relevance_score"], reverse=True)

    # --- Auto-like posts scoring 7+ ---
    likes_done = 0
    for post, eval_data in scored_posts:
        if likes_done >= MAX_LIKES_PER_RUN:
            break
        if eval_data["relevance_score"] < 7:
            continue

        if dry_run:
            log.info(f"[DRY RUN] Would like post by @{post.author_handle} (score {eval_data['relevance_score']})")
        else:
            await random_delay("like")
            success = await like_post(client, post.post_id)
            if success:
                likes_done += 1
                engagement_log.append({
                    "action": "like",
                    "post_id": post.post_id,
                    "author": post.author_handle,
                    "score": eval_data["relevance_score"],
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                })
                log.info(f"Liked post by @{post.author_handle} ({likes_done}/{MAX_LIKES_PER_RUN})")

    # --- Reply to top posts ---
    replies_done = 0
    replied_authors = set()  # avoid replying to same author twice
    for post, eval_data in scored_posts:
        if replies_done >= MAX_REPLIES_PER_RUN:
            break
        if eval_data["relevance_score"] < 7:
            continue
        if "reply" not in eval_data.get("suggested_actions", []):
            continue
        if post.author_handle in replied_authors:
            continue

        log.info(f"Drafting reply to @{post.author_handle}...")
        reply_text = await draft_reply(
            post_text=post.text,
            author=post.author_handle,
            niche_id=niche_id,
        )

        if reply_text:
            if dry_run:
                log.info(f"[DRY RUN] Would reply to @{post.author_handle}: {reply_text[:80]}...")
                replies_done += 1
                replied_authors.add(post.author_handle)
            else:
                await random_delay("reply")
                reply_id = await reply_to_post(client, post.post_id, reply_text)
                if reply_id:
                    replies_done += 1
                    replied_authors.add(post.author_handle)
                    engagement_log.append({
                        "action": "reply",
                        "post_id": post.post_id,
                        "reply_id": reply_id,
                        "author": post.author_handle,
                        "reply_text": reply_text,
                        "score": eval_data["relevance_score"],
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    })
                    log.info(f"Replied to @{post.author_handle} ({replies_done}/{MAX_REPLIES_PER_RUN}): {reply_text[:60]}...")

    # --- Follow a few relevant accounts ---
    followed_handles = {
        e["author"] for e in engagement_log if e.get("action") == "follow"
    }
    follows_done = 0
    seen_follow_handles = set()  # dedup within this run

    for post, eval_data in scored_posts:
        if follows_done >= MAX_FOLLOWS_PER_RUN:
            break
        if eval_data["relevance_score"] < 8:
            continue
        if "follow" not in eval_data.get("suggested_actions", []):
            continue
        if post.author_handle in followed_handles or post.author_handle in seen_follow_handles:
            continue

        seen_follow_handles.add(post.author_handle)
        if dry_run:
            log.info(f"[DRY RUN] Would follow @{post.author_handle}")
            follows_done += 1
        else:
            await random_delay("follow")
            success = await follow_user(client, post.author_handle)
            if success:
                follows_done += 1
                followed_handles.add(post.author_handle)
                engagement_log.append({
                    "action": "follow",
                    "post_id": post.post_id,
                    "author": post.author_handle,
                    "score": eval_data["relevance_score"],
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                })
                log.info(f"Followed @{post.author_handle} ({follows_done}/{MAX_FOLLOWS_PER_RUN})")

    # Save engagement log
    save_json(ENGAGEMENT_LOG, engagement_log)

    # Summary
    summary = (
        f"Done. Likes: {likes_done}, Replies: {replies_done}, "
        f"Follows: {follows_done}"
    )
    log.info(summary)

    notify("@tatamispaces engage", summary)


if __name__ == "__main__":
    asyncio.run(main())
