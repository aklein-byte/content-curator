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
import argparse
import time
from pathlib import Path
from datetime import datetime, timezone

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent))

from dotenv import load_dotenv
load_dotenv()

from tools.xapi import search_posts, like_post, follow_user, reply_to_post, XPost, get_liking_users, get_own_recent_tweets, get_user_recent_tweets, set_niche as set_xapi_niche
from tools.common import load_json, save_json, notify, acquire_lock, release_lock, setup_logging, load_config
from agents.engager import evaluate_post, draft_reply
from config.niches import get_niche

log = setup_logging("engage")

BASE_DIR = Path(__file__).parent
POSTS_FILE = Path(os.environ.get("POSTS_FILE", str(BASE_DIR / "posts.json")))
ENGAGEMENT_LOG = Path(os.environ.get("ENGAGEMENT_LOG", str(BASE_DIR / "engagement-log.json")))
ENGAGEMENT_DRAFTS = Path(os.environ.get("ENGAGEMENT_DRAFTS", str(BASE_DIR / "engagement-drafts.json")))

# Limits
MAX_LIKES_PER_RUN = 25
MAX_REPLIES_PER_RUN = 15
MAX_FOLLOWS_PER_RUN = 5
POSTS_PER_QUERY = 50

# Time budget — exit gracefully before orchestrator kills us
MAX_RUNTIME_SECONDS = 1000  # orchestrator timeout is 1200s, leave 200s buffer




def already_liked(log_entries: list, post_id: str) -> bool:
    """Check if we already liked this post."""
    return any(e["post_id"] == post_id and e["action"] == "like" for e in log_entries)


def already_replied(log_entries: list, post_id: str) -> bool:
    """Check if we already replied to this post."""
    return any(e["post_id"] == post_id and e["action"] == "reply" for e in log_entries)


def load_source_tweet_ids() -> set:
    """Load tweet IDs we've used as sources for our own posts.
    We shouldn't like/engage with these — it looks weird to like the original
    of content we've already curated and reposted."""
    ids = set()
    if POSTS_FILE.exists():
        try:
            data = json.loads(POSTS_FILE.read_text())
            for p in data.get("posts", []):
                src = p.get("source_url") or ""
                if src:
                    tweet_id = src.rstrip("/").split("/")[-1]
                    if tweet_id.isdigit():
                        ids.add(tweet_id)
        except Exception:
            pass
    return ids




def engage_back(eng_log: list, dry_run: bool = False) -> int:
    """Like tweets from users who recently liked our posts. Highest-ROI engagement."""
    log.info("Checking who engaged with our recent posts...")
    our_tweets = get_own_recent_tweets(max_results=10)
    if not our_tweets:
        log.info("  No recent tweets to check")
        return 0

    # Collect likers we haven't engaged back with
    already_liked_authors = {e.get("author_handle") or e.get("author") for e in eng_log if e.get("action") == "like"}
    likers = {}
    for tweet in our_tweets[:5]:  # Check last 5 posts (save API credits)
        users = get_liking_users(tweet["id"], max_results=10)
        for u in users:
            handle = u.get("username", "")
            if handle and handle not in likers and handle not in already_liked_authors:
                likers[handle] = u

    if not likers:
        log.info("  No new likers to engage back with")
        return 0

    log.info(f"  Found {len(likers)} users who liked our posts")

    # Like 1-2 of their recent tweets
    engaged = 0
    import random as _rand
    import time as _time
    for handle, user_info in list(likers.items())[:5]:
        user_id = user_info.get("id")
        if not user_id:
            continue
        their_tweets = get_user_recent_tweets(user_id, max_results=5)
        if not their_tweets:
            continue

        tweet_to_like = their_tweets[0]  # Most recent
        if dry_run:
            log.info(f"  [DRY] Would like @{handle}'s tweet: {tweet_to_like.get('text', '')[:50]}...")
            engaged += 1
        else:
            delay = _rand.randint(15, 45)
            log.info(f"  Waiting {delay}s before engaging back with @{handle}...")
            _time.sleep(delay)
            if like_post(tweet_to_like["id"]):
                engaged += 1
                eng_log.append({
                    "action": "like",
                    "post_id": tweet_to_like["id"],
                    "author_handle": handle,
                    "reason": "engage_back",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                })
                log.info(f"  Liked @{handle}'s tweet (engage-back {engaged})")

    log.info(f"  Engage-back: {engaged} likes")
    return engaged


async def main():
    global MAX_LIKES_PER_RUN, MAX_REPLIES_PER_RUN, MAX_FOLLOWS_PER_RUN

    parser = argparse.ArgumentParser(description="Engage with JP architecture posts")
    parser.add_argument("--niche", default="tatamispaces", help="Niche ID")
    parser.add_argument("--dry-run", action="store_true", help="Evaluate only, no actions")
    parser.add_argument("--max-likes", type=int, default=MAX_LIKES_PER_RUN, help="Max likes per run")
    parser.add_argument("--max-replies", type=int, default=MAX_REPLIES_PER_RUN, help="Max replies per run")
    parser.add_argument("--max-follows", type=int, default=MAX_FOLLOWS_PER_RUN, help="Max follows per run")
    args = parser.parse_args()

    MAX_LIKES_PER_RUN = args.max_likes
    MAX_REPLIES_PER_RUN = args.max_replies
    MAX_FOLLOWS_PER_RUN = args.max_follows

    niche_id = args.niche
    dry_run = args.dry_run
    niche = get_niche(niche_id)
    set_xapi_niche(niche_id)
    engagement_cfg = niche.get("engagement", {})
    queries = engagement_cfg.get("search_queries", [])

    if not queries:
        log.error(f"No search queries configured for niche '{niche_id}'")
        return

    log.info(f"Starting engagement for {niche['handle']} ({'DRY RUN' if dry_run else 'LIVE'})")

    start_time = time.time()

    def time_left() -> float:
        return MAX_RUNTIME_SECONDS - (time.time() - start_time)

    # No login needed — official API uses OAuth from .env

    # Load existing logs
    engagement_log = load_json(ENGAGEMENT_LOG)
    if not isinstance(engagement_log, list):
        engagement_log = []

    # Gather posts from all queries
    all_posts: list[XPost] = []
    seen_ids = set()
    min_likes = engagement_cfg.get("min_likes", 20)

    # Engage back with people who liked our posts (highest ROI)
    engage_back(engagement_log, dry_run)

    # Run a random subset of queries each run (covers different ground each time)
    max_queries = min(load_config().get("max_queries_per_run", 5), len(queries))
    shuffled_queries = random.sample(queries, max_queries)
    failed_searches = 0

    for i, query in enumerate(shuffled_queries):
        if time_left() < 120:
            log.warning(f"Time budget low ({time_left():.0f}s left), stopping searches")
            break

        log.info(f"Searching: {query[:60]}...")
        posts = search_posts(query, max_results=POSTS_PER_QUERY)

        if not posts:
            failed_searches += 1
            log.warning(f"Search returned 0 results ({failed_searches} failures)")
            if failed_searches >= 3:
                log.warning("3+ search failures, API issue — skipping remaining queries")
                break
            time.sleep(5)
            continue

        # Filter by min likes (API v2 doesn't support min_faves operator)
        before = len(posts)
        posts = [p for p in posts if p.likes >= min_likes]
        log.info(f"  {len(posts)}/{before} posts with >= {min_likes} likes")

        for p in posts:
            if p.post_id not in seen_ids:
                seen_ids.add(p.post_id)
                all_posts.append(p)

        # Brief delay between searches (API handles rate limiting, no need for long waits)
        if i < len(shuffled_queries) - 1:
            time.sleep(random.uniform(3, 8))

    log.info(f"Found {len(all_posts)} unique posts across {len(shuffled_queries)} queries")

    # Skip tweets we've used as sources for our own posts
    source_ids = load_source_tweet_ids()

    # Skip our own tweets (don't engage with ourselves)
    our_handle = niche["handle"].lstrip("@").lower()

    # Evaluate posts with Claude (shuffle so each run sees different posts first)
    random.shuffle(all_posts)
    scored_posts = []
    eval_count = 0
    for post in all_posts:
        if time_left() < 300:
            log.warning(f"Time budget low ({time_left():.0f}s), stopping evaluations ({eval_count} done)")
            break
        liked = already_liked(engagement_log, post.post_id)
        replied = already_replied(engagement_log, post.post_id)
        if liked and replied:
            continue  # Fully engaged, skip
        if post.author_handle.lower() == our_handle:
            log.info(f"  Skip @{post.author_handle} — our own tweet")
            continue
        if post.post_id in source_ids:
            log.info(f"  Skip @{post.author_handle} — source tweet for our content")
            continue

        eval_count += 1
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
    # Sort by relevance first, then by views (bigger audience = more exposure)
    scored_posts.sort(key=lambda x: (x[1]["relevance_score"], x[0].views), reverse=True)

    # --- Auto-like posts scoring 7+ ---
    likes_done = 0
    for post, eval_data in scored_posts:
        if likes_done >= MAX_LIKES_PER_RUN:
            break
        if time_left() < 60:
            log.warning(f"Time budget low ({time_left():.0f}s), stopping likes")
            break
        if eval_data["relevance_score"] < 5:
            continue
        if already_liked(engagement_log, post.post_id):
            continue

        if dry_run:
            log.info(f"[DRY RUN] Would like post by @{post.author_handle} (score {eval_data['relevance_score']})")
            likes_done += 1
        else:
            time.sleep(random.uniform(2, 8))
            success = like_post(post.post_id)
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
        if time_left() < 120:
            log.warning(f"Time budget low ({time_left():.0f}s), stopping replies")
            break
        if eval_data["relevance_score"] < 6:
            continue
        if already_replied(engagement_log, post.post_id):
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
                time.sleep(random.uniform(3, 10))
                reply_id = reply_to_post(post.post_id, reply_text)
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
        if time_left() < 30:
            log.warning(f"Time budget low ({time_left():.0f}s), stopping follows")
            break
        if eval_data["relevance_score"] < 7:
            continue
        if post.author_handle in followed_handles or post.author_handle in seen_follow_handles:
            continue

        seen_follow_handles.add(post.author_handle)
        if dry_run:
            log.info(f"[DRY RUN] Would follow @{post.author_handle}")
            follows_done += 1
        else:
            time.sleep(random.uniform(2, 8))
            success = follow_user(post.author_id)
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
    elapsed = int(time.time() - start_time)
    summary = (
        f"Done in {elapsed}s. Likes: {likes_done}, Replies: {replies_done}, "
        f"Follows: {follows_done}"
    )
    log.info(summary)

    notify("@tatamispaces engage", summary)


if __name__ == "__main__":
    lock_fd = acquire_lock(BASE_DIR / ".engage.lock")
    if not lock_fd:
        log.info("Another engage.py is already running, exiting")
        sys.exit(0)
    try:
        asyncio.run(main())
    finally:
        release_lock(lock_fd)
