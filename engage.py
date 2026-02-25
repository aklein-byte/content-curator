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
from tools.common import load_json, save_json, notify, acquire_lock, release_lock, setup_logging, load_config, niche_log_path
from tools.post_queue import load_posts as pq_load_posts, save_posts as pq_save_posts, next_post_id
from agents.engager import evaluate_post, draft_reply, draft_quote_tweet
from config.niches import get_niche

# Daily caps — hard limits across all runs (overridable per niche in _apply_niche_limits)
DAILY_MAX_REPLIES = 15
DAILY_MAX_LIKES = 30
DAILY_MAX_FOLLOWS = 6

# Minimum author followers to reply to (smaller accounts = wasted visibility)
MIN_AUTHOR_FOLLOWERS_FOR_REPLY = 300

# Minimum post likes to reply to (more eyeballs on our reply)
MIN_POST_LIKES_FOR_REPLY = 3

# Per-niche engagement limits — conservative for new accounts, higher for established
_NICHE_ENGAGE_LIMITS = {
    "tatamispaces": {
        "daily_max_replies": 30,
        "daily_max_likes": 60,
        "daily_max_follows": 10,
        "min_author_followers_for_reply": 300,
        "min_post_likes_for_reply": 3,
        "like_delay": (10, 35),      # seconds (min, max)
        "reply_delay": (30, 120),
        "follow_delay": (20, 60),
    },
    "museumstories": {
        "daily_max_replies": 20,
        "daily_max_likes": 40,
        "daily_max_follows": 8,
        "min_author_followers_for_reply": 500,
        "min_post_likes_for_reply": 10,
        "like_delay": (15, 45),
        "reply_delay": (45, 180),
        "follow_delay": (25, 75),
    },
}

def _apply_niche_limits(niche_id: str):
    """Apply niche-specific engagement limits."""
    global DAILY_MAX_REPLIES, DAILY_MAX_LIKES, DAILY_MAX_FOLLOWS
    global MIN_AUTHOR_FOLLOWERS_FOR_REPLY, MIN_POST_LIKES_FOR_REPLY
    limits = _NICHE_ENGAGE_LIMITS.get(niche_id, {})
    DAILY_MAX_REPLIES = limits.get("daily_max_replies", DAILY_MAX_REPLIES)
    DAILY_MAX_LIKES = limits.get("daily_max_likes", DAILY_MAX_LIKES)
    DAILY_MAX_FOLLOWS = limits.get("daily_max_follows", DAILY_MAX_FOLLOWS)
    MIN_AUTHOR_FOLLOWERS_FOR_REPLY = limits.get("min_author_followers_for_reply", MIN_AUTHOR_FOLLOWERS_FOR_REPLY)
    MIN_POST_LIKES_FOR_REPLY = limits.get("min_post_likes_for_reply", MIN_POST_LIKES_FOR_REPLY)

log = setup_logging("engage")

BASE_DIR = Path(__file__).parent

# Resolved per-niche in main()
_niche_id: str | None = None
ENGAGEMENT_LOG: Path = BASE_DIR / "engagement-log.json"
ENGAGEMENT_DRAFTS: Path = BASE_DIR / "engagement-drafts.json"

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


def count_today_actions(log_entries: list, action: str) -> int:
    """Count how many of a given action type were taken today (UTC)."""
    today = datetime.now(timezone.utc).date().isoformat()
    return sum(
        1 for e in log_entries
        if e.get("action") == action and e.get("timestamp", "").startswith(today)
    )


def replies_to_author_this_week(log_entries: list, author: str) -> int:
    """Count replies to a specific author in the last 7 days."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    return sum(
        1 for e in log_entries
        if e.get("action") == "reply"
        and e.get("author", "").lower() == author.lower()
        and e.get("timestamp", "") >= cutoff
    )


def _post_age_minutes(post) -> float:
    """Return post age in minutes. Returns 9999 if created_at is missing."""
    if not post.created_at:
        return 9999
    try:
        created = datetime.fromisoformat(post.created_at.replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - created).total_seconds() / 60
    except Exception:
        return 9999


def track_reply_performance(eng_log: list, niche_id: str) -> None:
    """Check how our recent replies performed (likes, reply-backs).
    Updates engagement log entries in-place with performance data.
    Runs once per engage session on unchecked replies older than 1 hour."""
    import requests
    from tools.xapi import _get_auth, set_niche

    # Find replies that haven't been checked yet and are >1 hour old
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    unchecked = [
        e for e in eng_log
        if e.get("action") == "reply"
        and e.get("reply_id")
        and "reply_likes" not in e  # not yet checked
        and e.get("timestamp", "") < cutoff  # at least 1 hour old
        and e.get("timestamp", "") > week_ago  # only last 7 days
    ]

    if not unchecked:
        return

    # Check up to 50 per run (API limit is 100 per request)
    batch = unchecked[:50]
    ids = [str(e["reply_id"]) for e in batch]

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
    for tw in data["data"]:
        m = tw["public_metrics"]
        entry = next((e for e in batch if str(e["reply_id"]) == tw["id"]), None)
        if entry:
            entry["reply_likes"] = m["like_count"]
            entry["reply_replies"] = m["reply_count"]
            entry["reply_retweets"] = m["retweet_count"]
            entry["checked_at"] = datetime.now(timezone.utc).isoformat()
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
    global _niche_id, ENGAGEMENT_LOG, ENGAGEMENT_DRAFTS

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

    # Apply niche-specific engagement limits
    _apply_niche_limits(niche_id)

    # Resolve niche-specific file paths
    _niche_id = niche_id
    ENGAGEMENT_LOG = Path(os.environ.get("ENGAGEMENT_LOG", str(niche_log_path("engagement-log.json", niche_id))))
    ENGAGEMENT_DRAFTS = Path(os.environ.get("ENGAGEMENT_DRAFTS", str(niche_log_path("engagement-drafts.json", niche_id))))

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

    # Track how our recent replies performed (feedback loop)
    track_reply_performance(engagement_log, niche_id)

    # Gather posts from all queries
    all_posts: list[XPost] = []
    seen_ids = set()
    min_likes = engagement_cfg.get("min_likes", 20)

    # Engage back with people who liked our posts (highest ROI)
    engage_back(engagement_log, dry_run)

    # Select queries — weighted by past performance if insights exist,
    # with 1 slot reserved for random exploration
    max_queries = min(load_config().get("max_queries_per_run", 5), len(queries))
    insights_path = BASE_DIR / "data" / f"insights-{niche_id}.json"
    query_perf = load_json(insights_path, default={}).get("query_performance", {})

    # Also read recommended_min_likes from insights
    rec_min = load_json(insights_path, default={}).get("recommended_min_likes")
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
    daily_likes = count_today_actions(engagement_log, "like")
    for post, eval_data in scored_posts:
        if likes_done >= MAX_LIKES_PER_RUN:
            break
        if daily_likes + likes_done >= DAILY_MAX_LIKES:
            log.info(f"Daily like cap reached ({DAILY_MAX_LIKES}), stopping likes")
            break
        if time_left() < 60:
            log.warning(f"Time budget low ({time_left():.0f}s), stopping likes")
            break
        if eval_data["relevance_score"] < 6:
            continue
        if already_liked(engagement_log, post.post_id):
            continue

        if dry_run:
            log.info(f"[DRY RUN] Would like post by @{post.author_handle} (score {eval_data['relevance_score']})")
            likes_done += 1
        else:
            like_delay = _NICHE_ENGAGE_LIMITS.get(niche_id, {}).get("like_delay", (15, 45))
            time.sleep(random.uniform(*like_delay))
            success = like_post(post.post_id)
            if success:
                likes_done += 1
                engagement_log.append({
                    "action": "like",
                    "post_id": post.post_id,
                    "author": post.author_handle,
                    "score": eval_data["relevance_score"],
                    "post_likes": post.likes,
                    "author_followers": post.author_followers,
                    "query": getattr(post, '_source_query', None),
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                })
                log.info(f"Liked post by @{post.author_handle} ({likes_done}/{MAX_LIKES_PER_RUN})")

    # --- Reply to top posts ---
    replies_done = 0
    daily_replies = count_today_actions(engagement_log, "reply")
    replied_authors = set()  # avoid replying to same author twice
    for post, eval_data in scored_posts:
        if replies_done >= MAX_REPLIES_PER_RUN:
            break
        if daily_replies + replies_done >= DAILY_MAX_REPLIES:
            log.info(f"Daily reply cap reached ({DAILY_MAX_REPLIES}), stopping replies")
            break
        if time_left() < 120:
            log.warning(f"Time budget low ({time_left():.0f}s), stopping replies")
            break
        if eval_data["relevance_score"] < 8:
            continue
        if already_replied(engagement_log, post.post_id):
            continue
        if post.author_handle in replied_authors:
            continue
        # Per-account cooldown: max 2 replies per author per week
        if replies_to_author_this_week(engagement_log, post.author_handle) >= 2:
            continue
        # Only reply to accounts with enough followers for visibility
        if post.author_followers < MIN_AUTHOR_FOLLOWERS_FOR_REPLY:
            log.info(f"  Skip @{post.author_handle} — {post.author_followers} followers (need {MIN_AUTHOR_FOLLOWERS_FOR_REPLY}+)")
            continue
        # Only reply to posts with enough likes (more eyeballs)
        if post.likes < MIN_POST_LIKES_FOR_REPLY:
            log.info(f"  Skip @{post.author_handle} — {post.likes} likes (need {MIN_POST_LIKES_FOR_REPLY}+)")
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
                reply_delay = _NICHE_ENGAGE_LIMITS.get(niche_id, {}).get("reply_delay", (45, 180))
                time.sleep(random.uniform(*reply_delay))
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
                        "post_likes": post.likes,
                        "author_followers": post.author_followers,
                        "query": getattr(post, '_source_query', None),
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    })
                    log.info(f"Replied to @{post.author_handle} ({replies_done}/{MAX_REPLIES_PER_RUN}): {reply_text[:60]}...")

    # --- Follow a few relevant accounts ---
    followed_handles = {
        e["author"] for e in engagement_log if e.get("action") == "follow"
    }
    follows_done = 0
    daily_follows = count_today_actions(engagement_log, "follow")
    seen_follow_handles = set()  # dedup within this run

    for post, eval_data in scored_posts:
        if follows_done >= MAX_FOLLOWS_PER_RUN:
            break
        if daily_follows + follows_done >= DAILY_MAX_FOLLOWS:
            log.info(f"Daily follow cap reached ({DAILY_MAX_FOLLOWS}), stopping follows")
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
            follow_delay = _NICHE_ENGAGE_LIMITS.get(niche_id, {}).get("follow_delay", (20, 60))
            time.sleep(random.uniform(*follow_delay))
            success = follow_user(post.author_id)
            if success:
                follows_done += 1
                followed_handles.add(post.author_handle)
                engagement_log.append({
                    "action": "follow",
                    "post_id": post.post_id,
                    "author": post.author_handle,
                    "score": eval_data["relevance_score"],
                    "post_likes": post.likes,
                    "author_followers": post.author_followers,
                    "query": getattr(post, '_source_query', None),
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                })
                log.info(f"Followed @{post.author_handle} ({follows_done}/{MAX_FOLLOWS_PER_RUN})")

    # --- Draft quote tweets for top posts (saved to posts file for review) ---
    quotes_drafted = 0
    MAX_QUOTES_PER_RUN = 1
    MIN_LIKES_FOR_QUOTE = 500  # only quote posts with broad reach
    MIN_SCORE_FOR_QUOTE = 9

    # Load posts file to check for existing quote drafts
    posts_data = pq_load_posts(_niche_id)
    existing_quote_ids = {
        str(p.get("quote_tweet_id")) for p in posts_data.get("posts", [])
        if p.get("quote_tweet_id")
    }

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
            new_post = {
                "id": next_post_id(posts_data),
                "status": "draft",
                "type": "quote",
                "text": qt_text,
                "quote_tweet_id": str(post.post_id),
                "quote_author": post.author_handle,
                "quote_text": post.text[:200],
                "quote_likes": post.likes,
                "source": "engage",
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
            posts_data["posts"].append(new_post)
            pq_save_posts(posts_data, _niche_id, lock=True)
            quotes_drafted += 1
            log.info(f"Quote tweet draft #{new_post['id']}: {qt_text[:80]}...")

    # Save engagement log
    save_json(ENGAGEMENT_LOG, engagement_log)

    # Summary
    elapsed = int(time.time() - start_time)
    parts = [f"Likes: {likes_done}", f"Replies: {replies_done}", f"Follows: {follows_done}"]
    if quotes_drafted:
        parts.append(f"Quote drafts: {quotes_drafted}")
    summary = f"Done in {elapsed}s. {', '.join(parts)}"
    log.info(summary)

    notify(f"{niche['handle']} engage", summary)


if __name__ == "__main__":
    lock_fd = acquire_lock(BASE_DIR / ".engage.lock")
    if not lock_fd:
        log.info("Another engage.py is already running, exiting")
        sys.exit(0)
    try:
        asyncio.run(main())
    finally:
        release_lock(lock_fd)
