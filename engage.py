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
from datetime import datetime, timezone, timedelta

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent))

from dotenv import load_dotenv
load_dotenv()

from tools.xapi import search_posts, like_post, follow_user, reply_to_post, XPost, get_liking_users, get_own_recent_tweets, get_user_recent_tweets, set_niche as set_xapi_niche
from tools.common import notify, setup_logging, load_config
from tools.post_queue import load_posts as pq_load_posts, save_posts as pq_save_posts, next_post_id, insert_post as pq_insert_post
from tools.db import (
    log_engagement, already_engaged as db_already_engaged,
    count_today_actions as db_count_today, replies_to_author_this_week as db_replies_week,
    update_engagement_entry, get_engaged_authors, get_insights,
)
from agents.engager import evaluate_post, draft_reply, draft_quote_tweet
from config.niches import get_niche

# Default engagement limits (overridden per-niche via engage_limits in config/niches.py)
_DEFAULT_ENGAGE_LIMITS = {
    "daily_max_replies": 15,
    "daily_max_likes": 30,
    "daily_max_follows": 6,
    "min_author_followers_for_reply": 300,
    "min_post_likes_for_reply": 3,
    "like_delay": [15, 45],
    "reply_delay": [45, 180],
    "follow_delay": [20, 60],
}


def _get_limits(niche_id: str) -> dict:
    """Get engagement limits for a niche (from config, with defaults)."""
    niche = get_niche(niche_id)
    limits = niche.get("engage_limits", {})
    merged = dict(_DEFAULT_ENGAGE_LIMITS)
    merged.update(limits)
    return merged

log = setup_logging("engage")

BASE_DIR = Path(__file__).parent

# Resolved per-niche in main()
_niche_id: str | None = None

# Limits
MAX_LIKES_PER_RUN = 25
MAX_REPLIES_PER_RUN = 15
MAX_FOLLOWS_PER_RUN = 5
POSTS_PER_QUERY = 50

# Time budget — exit gracefully before orchestrator kills us
MAX_RUNTIME_SECONDS = 1000  # orchestrator timeout is 1200s, leave 200s buffer




def already_liked(post_id: str) -> bool:
    """Check if we already liked this post."""
    return db_already_engaged(_niche_id, "x", "like", post_id)


def already_replied(post_id: str) -> bool:
    """Check if we already replied to this post."""
    return db_already_engaged(_niche_id, "x", "reply", post_id)


def count_today_actions(action: str) -> int:
    """Count how many of a given action type were taken today (UTC)."""
    return db_count_today(_niche_id, "x", action)


def replies_to_author_this_week(author: str) -> int:
    """Count replies to a specific author in the last 7 days."""
    return db_replies_week(_niche_id, "x", author)


def _post_age_minutes(post) -> float:
    """Return post age in minutes. Returns 9999 if created_at is missing."""
    if not post.created_at:
        return 9999
    try:
        created = datetime.fromisoformat(post.created_at.replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - created).total_seconds() / 60
    except Exception:
        return 9999


def track_reply_performance(niche_id: str) -> None:
    """Check how our recent replies performed (likes, reply-backs).
    Updates engagement log entries in DB with performance data.
    Runs once per engage session on unchecked replies older than 1 hour."""
    import requests
    from tools.xapi import _get_auth, set_niche
    from tools.db import get_db

    db = get_db()
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()

    unchecked = db.execute(
        """SELECT id, reply_id FROM engagement_log
           WHERE niche_id = ? AND platform = 'x' AND action = 'reply'
           AND reply_id IS NOT NULL AND reply_likes IS NULL
           AND timestamp < ? AND timestamp > ?
           LIMIT 50""",
        (niche_id, cutoff, week_ago),
    ).fetchall()

    if not unchecked:
        return

    ids = [str(r["reply_id"]) for r in unchecked]

    try:
        set_niche(niche_id)
        auth = _get_auth()
        resp = requests.get(
            "https://api.x.com/2/tweets",
            params={"ids": ",".join(ids), "tweet.fields": "public_metrics"},
            auth=auth,
            timeout=15,
        )
        data = resp.json()
    except Exception as ex:
        log.warning(f"Reply performance check failed: {ex}")
        return

    if "data" not in data:
        return

    checked = 0
    got_engagement = 0
    now_iso = datetime.now(timezone.utc).isoformat()
    for tw in data["data"]:
        m = tw["public_metrics"]
        row = next((r for r in unchecked if str(r["reply_id"]) == tw["id"]), None)
        if row:
            update_engagement_entry(
                row["id"],
                reply_likes=m["like_count"],
                reply_replies=m["reply_count"],
                reply_retweets=m["retweet_count"],
                checked_at=now_iso,
            )
            checked += 1
            if m["like_count"] > 0 or m["reply_count"] > 0:
                got_engagement += 1

    if checked:
        log.info(f"Reply performance: {got_engagement}/{checked} got engagement (checked {checked} unchecked replies)")


def load_source_tweet_ids() -> set:
    """Load tweet IDs we've used as sources for our own posts.
    We shouldn't like/engage with these — it looks weird to like the original
    of content we've already curated and reposted."""
    ids = set()
    try:
        data = pq_load_posts(_niche_id)
        for p in data.get("posts", []):
            src = p.get("source_url") or ""
            if src:
                tweet_id = src.rstrip("/").split("/")[-1]
                if tweet_id.isdigit():
                    ids.add(tweet_id)
    except Exception:
        pass
    return ids




def engage_back(dry_run: bool = False) -> int:
    """Like tweets from users who recently liked our posts. Highest-ROI engagement."""
    log.info("Checking who engaged with our recent posts...")
    our_tweets = get_own_recent_tweets(max_results=10)
    if not our_tweets:
        log.info("  No recent tweets to check")
        return 0

    # Collect likers we haven't engaged back with
    already_liked_authors = get_engaged_authors(_niche_id, "x", "like")

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
            delay = random.randint(15, 45)
            log.info(f"  Waiting {delay}s before engaging back with @{handle}...")
            time.sleep(delay)
            if like_post(tweet_to_like["id"]):
                engaged += 1
                log_engagement(
                    _niche_id, "x", "like",
                    post_id=tweet_to_like["id"],
                    author_handle=handle,
                    reason="engage_back",
                )
                log.info(f"  Liked @{handle}'s tweet (engage-back {engaged})")

    log.info(f"  Engage-back: {engaged} likes")
    return engaged


async def main():
    global MAX_LIKES_PER_RUN, MAX_REPLIES_PER_RUN, MAX_FOLLOWS_PER_RUN
    global _niche_id

    parser = argparse.ArgumentParser(description="Engage with JP architecture posts")
    parser.add_argument("--niche", default="tatamispaces", help="Niche ID")
    parser.add_argument("--dry-run", action="store_true", help="Evaluate only, no actions")
    parser.add_argument("--max-likes", type=int, default=MAX_LIKES_PER_RUN, help="Max likes per run")
    parser.add_argument("--max-replies", type=int, default=MAX_REPLIES_PER_RUN, help="Max replies per run")
    parser.add_argument("--max-follows", type=int, default=MAX_FOLLOWS_PER_RUN, help="Max follows per run")
    args = parser.parse_args()

    max_likes = args.max_likes
    max_replies = args.max_replies
    max_follows = args.max_follows

    niche_id = args.niche
    dry_run = args.dry_run
    niche = get_niche(niche_id)
    set_xapi_niche(niche_id)

    # Load niche-specific engagement limits from config
    limits = _get_limits(niche_id)

    _niche_id = niche_id

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

    # Track how our recent replies performed (feedback loop)
    track_reply_performance(niche_id)

    # Gather posts from all queries
    all_posts: list[XPost] = []
    seen_ids = set()
    min_likes = engagement_cfg.get("min_likes", 20)

    # Engage back with people who liked our posts (highest ROI)
    engage_back(dry_run)

    # Select queries — weighted by past performance if insights exist,
    # with 1 slot reserved for random exploration
    max_queries = min(load_config().get("max_queries_per_run", 5), len(queries))
    _insights = get_insights(niche_id)
    query_perf = _insights.get("query_performance", {})

    # Also read recommended_min_likes from insights
    rec_min = _insights.get("recommended_min_likes")
    if rec_min is not None:
        old_min = min_likes
        min_likes = min(rec_min, 15)  # Cap at 15 — higher kills discovery
        log.info(f"Using learned min_likes={min_likes} (was {old_min})")

    if query_perf and max_queries >= 2:
        # Weighted selection: score each query, pick top (max_queries - 1),
        # reserve 1 slot for a random unexplored/low-data query
        weights = {}
        for q in queries:
            stats = query_perf.get(q)
            if stats and stats.get("posts_engaged", 0) >= 3:
                w = (stats.get("reply_engagement_rate", 0) * 3) + (stats.get("avg_score", 0) / 10) + 0.5
                weights[q] = max(w, 0.3)
            else:
                weights[q] = 0.3  # unexplored queries get base weight

        weighted_queries = list(weights.keys())
        w_values = [weights[q] for q in weighted_queries]

        # Pick (max_queries - 1) by weight, 1 random exploration
        n_weighted = max_queries - 1
        selected = set()
        if n_weighted > 0:
            chosen = random.choices(weighted_queries, weights=w_values, k=n_weighted * 3)
            for q in chosen:
                if q not in selected:
                    selected.add(q)
                if len(selected) >= n_weighted:
                    break

        # Exploration slot: pick from queries NOT already selected, prefer low-data
        remaining = [q for q in queries if q not in selected]
        if remaining:
            selected.add(random.choice(remaining))

        shuffled_queries = list(selected)[:max_queries]
        log.info(f"Query selection: {len(shuffled_queries)} queries ({len(query_perf)} with perf data)")
    else:
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
                p._source_query = query
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
        liked = already_liked(post.post_id)
        replied = already_replied(post.post_id)
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

    # Sort by relevance score descending, with recency boost
    # Fresh posts (<30 min) get +2 effective score, <60 min get +1
    # This prioritizes replying to fresh content where our reply gets max visibility
    def _sort_key(item):
        post, eval_data = item
        score = eval_data["relevance_score"]
        age = _post_age_minutes(post)
        if age < 30:
            score += 2
        elif age < 60:
            score += 1
        return (score, post.views)
    scored_posts.sort(key=_sort_key, reverse=True)

    # --- Auto-like posts scoring 6+ ---
    likes_done = 0
    daily_likes = count_today_actions("like")
    daily_max_likes = limits["daily_max_likes"]
    for post, eval_data in scored_posts:
        if likes_done >= max_likes:
            break
        if daily_likes + likes_done >= daily_max_likes:
            log.info(f"Daily like cap reached ({daily_max_likes}), stopping likes")
            break
        if time_left() < 60:
            log.warning(f"Time budget low ({time_left():.0f}s), stopping likes")
            break
        if eval_data["relevance_score"] < 6:
            continue
        if already_liked(post.post_id):
            continue

        if dry_run:
            log.info(f"[DRY RUN] Would like post by @{post.author_handle} (score {eval_data['relevance_score']})")
            likes_done += 1
        else:
            time.sleep(random.uniform(*limits["like_delay"]))
            success = like_post(post.post_id)
            if success:
                likes_done += 1
                log_engagement(
                    _niche_id, "x", "like",
                    post_id=post.post_id,
                    author=post.author_handle,
                    score=eval_data["relevance_score"],
                    post_likes=post.likes,
                    author_followers=post.author_followers,
                    query=getattr(post, '_source_query', None),
                )
                log.info(f"Liked post by @{post.author_handle} ({likes_done}/{max_likes})")

    # --- Reply to top posts ---
    replies_done = 0
    daily_replies = count_today_actions("reply")
    daily_max_replies = limits["daily_max_replies"]
    min_followers = limits["min_author_followers_for_reply"]
    min_post_likes = limits["min_post_likes_for_reply"]
    replied_authors = set()  # avoid replying to same author twice
    for post, eval_data in scored_posts:
        if replies_done >= max_replies:
            break
        if daily_replies + replies_done >= daily_max_replies:
            log.info(f"Daily reply cap reached ({daily_max_replies}), stopping replies")
            break
        if time_left() < 120:
            log.warning(f"Time budget low ({time_left():.0f}s), stopping replies")
            break
        if eval_data["relevance_score"] < 8:
            continue
        if already_replied(post.post_id):
            continue
        if post.author_handle in replied_authors:
            continue
        # Per-account cooldown: max 2 replies per author per week
        if replies_to_author_this_week(post.author_handle) >= 2:
            continue
        # Only reply to accounts with enough followers for visibility
        if post.author_followers < min_followers:
            log.info(f"  Skip @{post.author_handle} — {post.author_followers} followers (need {min_followers}+)")
            continue
        # Only reply to posts with enough likes (more eyeballs)
        if post.likes < min_post_likes:
            log.info(f"  Skip @{post.author_handle} — {post.likes} likes (need {min_post_likes}+)")
            continue

        log.info(f"Drafting reply to @{post.author_handle} ({post.author_followers} followers, {post.likes} likes)...")
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
                time.sleep(random.uniform(*limits["reply_delay"]))
                reply_id = reply_to_post(post.post_id, reply_text)
                if reply_id:
                    replies_done += 1
                    replied_authors.add(post.author_handle)
                    log_engagement(
                        _niche_id, "x", "reply",
                        post_id=post.post_id,
                        reply_id=reply_id,
                        author=post.author_handle,
                        reply_text=reply_text,
                        score=eval_data["relevance_score"],
                        post_likes=post.likes,
                        author_followers=post.author_followers,
                        query=getattr(post, '_source_query', None),
                    )
                    log.info(f"Replied to @{post.author_handle} ({replies_done}/{max_replies}): {reply_text[:60]}...")

    # --- Follow a few relevant accounts ---
    followed_handles = get_engaged_authors(_niche_id, "x", "follow")
    follows_done = 0
    daily_follows = count_today_actions("follow")
    daily_max_follows = limits["daily_max_follows"]
    seen_follow_handles = set()  # dedup within this run

    for post, eval_data in scored_posts:
        if follows_done >= max_follows:
            break
        if daily_follows + follows_done >= daily_max_follows:
            log.info(f"Daily follow cap reached ({daily_max_follows}), stopping follows")
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
            time.sleep(random.uniform(*limits["follow_delay"]))
            success = follow_user(post.author_id)
            if success:
                follows_done += 1
                followed_handles.add(post.author_handle)
                log_engagement(
                    _niche_id, "x", "follow",
                    post_id=post.post_id,
                    author=post.author_handle,
                    score=eval_data["relevance_score"],
                    post_likes=post.likes,
                    author_followers=post.author_followers,
                    query=getattr(post, '_source_query', None),
                )
                log.info(f"Followed @{post.author_handle} ({follows_done}/{max_follows})")

    # --- Draft quote tweets for top posts (saved to posts file for review) ---
    quotes_drafted = 0
    MAX_QUOTES_PER_RUN = 1
    MIN_LIKES_FOR_QUOTE = 500  # only quote posts with broad reach
    MIN_SCORE_FOR_QUOTE = 9

    # Check existing quote drafts via DB
    from tools.db import get_db as _gdb
    _qt_rows = _gdb().execute(
        "SELECT quote_tweet_id FROM posts WHERE niche_id = ? AND quote_tweet_id IS NOT NULL",
        (_niche_id,),
    ).fetchall()
    existing_quote_ids = {str(r["quote_tweet_id"]) for r in _qt_rows}

    for post, eval_data in scored_posts:
        if quotes_drafted >= MAX_QUOTES_PER_RUN:
            break
        if time_left() < 120:
            break
        if eval_data["relevance_score"] < MIN_SCORE_FOR_QUOTE:
            continue
        if post.likes < MIN_LIKES_FOR_QUOTE:
            continue
        if str(post.post_id) in existing_quote_ids:
            continue

        log.info(f"Drafting quote tweet for @{post.author_handle} ({post.likes} likes, score {eval_data['relevance_score']})...")
        qt_text = await draft_quote_tweet(
            post_text=post.text,
            author=post.author_handle,
            niche_id=niche_id,
            tweet_id=post.post_id,
        )

        if qt_text:
            # Extract image URLs from the original post if available
            qt_image_urls = post.image_urls or []
            new_post = {
                "status": "draft",
                "type": "quote",
                "title": f"QT @{post.author_handle}",
                "text": qt_text,
                "tweets": [{"text": qt_text, "images": []}],
                "image_urls": qt_image_urls,
                "quote_tweet_id": str(post.post_id),
                "quote_author": post.author_handle,
                "quote_text": post.text[:200],
                "quote_likes": post.likes,
                "source": "engage",
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
            new_id = pq_insert_post(_niche_id, new_post)
            quotes_drafted += 1
            log.info(f"Quote tweet draft #{new_id}: {qt_text[:80]}...")

    # Engagement log is already saved per-action via log_engagement()

    # Summary
    elapsed = int(time.time() - start_time)
    parts = [f"Likes: {likes_done}", f"Replies: {replies_done}", f"Follows: {follows_done}"]
    if quotes_drafted:
        parts.append(f"Quote drafts: {quotes_drafted}")
    summary = f"Done in {elapsed}s. {', '.join(parts)}"
    log.info(summary)

    notify(f"{niche['handle']} engage", summary)


if __name__ == "__main__":
    from tools.db import acquire_process_lock, release_process_lock
    _pre = argparse.ArgumentParser(add_help=False)
    _pre.add_argument("--niche", default="tatamispaces")
    _pre_args, _ = _pre.parse_known_args()
    lock_name = f"engage_{_pre_args.niche}"

    if not acquire_process_lock(lock_name):
        log.info("Another engage.py is already running for this niche, exiting")
        sys.exit(0)
    try:
        asyncio.run(main())
    finally:
        release_process_lock(lock_name)
