"""
Bluesky engagement script — search, evaluate, like, reply, follow.
Mirrors engage.py but for AT Protocol. More aggressive defaults since
Bluesky has no free-tier throttling.

Usage: python bluesky_engage.py [--niche tatamispaces] [--dry-run]
"""

import sys
import os
import asyncio
import random
import argparse
import time
from pathlib import Path
from datetime import datetime, timezone, timedelta

sys.path.insert(0, str(Path(__file__).parent))

from dotenv import load_dotenv
load_dotenv()

from tools.bluesky import (
    search_posts, like_post, follow_user, reply_to_post, get_profile,
    BskyPost, set_niche as set_bsky_niche, rate_budget_remaining,
    _clean_query_for_bluesky,
)
from tools.common import load_json, save_json, notify, acquire_lock, release_lock, setup_logging, load_config, niche_log_path
from tools.post_queue import load_posts as pq_load_posts
from agents.engager import evaluate_post, draft_reply
from config.niches import get_niche

# Default Bluesky engagement limits (more aggressive than X)
_DEFAULT_BSKY_LIMITS = {
    "daily_max_likes": 50,
    "daily_max_replies": 20,
    "daily_max_follows": 10,
    "min_author_followers_for_reply": 50,
    "min_post_likes_for_reply": 1,
    "like_delay": [5, 20],
    "reply_delay": [20, 90],
    "follow_delay": [15, 45],
}

log = setup_logging("bluesky_engage")

BASE_DIR = Path(__file__).parent

MAX_LIKES_PER_RUN = 25
MAX_REPLIES_PER_RUN = 15
MAX_FOLLOWS_PER_RUN = 5
POSTS_PER_QUERY = 25
MAX_RUNTIME_SECONDS = 800  # orchestrator timeout is 900s


def _get_limits(niche_id: str) -> dict:
    """Get Bluesky engagement limits for a niche."""
    niche = get_niche(niche_id)
    limits = niche.get("bluesky_engage_limits", {})
    merged = dict(_DEFAULT_BSKY_LIMITS)
    merged.update(limits)
    return merged


def _get_search_queries(niche_id: str) -> list[str]:
    """Get Bluesky-adapted search queries for a niche.

    Uses bluesky_search_queries if defined, otherwise adapts X queries.
    """
    niche = get_niche(niche_id)
    engagement = niche.get("engagement", {})

    # Prefer explicit Bluesky queries
    bsky_queries = engagement.get("bluesky_search_queries")
    if bsky_queries:
        return bsky_queries

    # Adapt X queries by stripping operators
    x_queries = engagement.get("search_queries", [])
    seen = set()
    cleaned = []
    for q in x_queries:
        c = _clean_query_for_bluesky(q)
        if c and c not in seen:
            seen.add(c)
            cleaned.append(c)
    return cleaned


def already_liked(log_entries: list, post_uri: str) -> bool:
    return any(e["post_uri"] == post_uri and e["action"] == "like" for e in log_entries)


def already_replied(log_entries: list, post_uri: str) -> bool:
    return any(e["post_uri"] == post_uri and e["action"] == "reply" for e in log_entries)


def count_today_actions(log_entries: list, action: str) -> int:
    today = datetime.now(timezone.utc).date().isoformat()
    return sum(
        1 for e in log_entries
        if e.get("action") == action and e.get("timestamp", "").startswith(today)
    )


def replies_to_author_this_week(log_entries: list, author: str) -> int:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    return sum(
        1 for e in log_entries
        if e.get("action") == "reply"
        and e.get("author", "").lower() == author.lower()
        and e.get("timestamp", "") >= cutoff
    )


def _post_age_minutes(post: BskyPost) -> float:
    if not post.created_at:
        return 9999
    try:
        created = datetime.fromisoformat(post.created_at.replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - created).total_seconds() / 60
    except Exception:
        return 9999


def _try_claude_eval(post: BskyPost, niche_id: str) -> dict | None:
    """Try evaluating via headless Claude CLI (cheap). Returns dict or None."""
    try:
        from tools.claude_runner import claude_json
        niche = get_niche(niche_id)
        system = f"""You evaluate Bluesky posts for {niche['handle']} ({niche['description']}).
Score 1-10 on relevance. Return JSON:
{{"relevance_score": 8, "should_engage": true, "reason": "Brief", "suggested_actions": ["like","reply"]}}"""
        prompt = f"Author: @{post.author_handle}\nText: {post.text}\nImages: {post.image_count}\nLikes: {post.likes}, Reposts: {post.reposts}"
        result = claude_json(system, prompt, timeout=30)
        if result and "relevance_score" in result:
            return {
                "relevance_score": result.get("relevance_score", 5),
                "should_engage": result.get("should_engage", False),
                "reason": result.get("reason", ""),
                "suggested_actions": result.get("suggested_actions", []),
            }
    except Exception as e:
        log.debug(f"Claude CLI eval failed: {e}")
    return None


async def main():
    parser = argparse.ArgumentParser(description="Engage on Bluesky")
    parser.add_argument("--niche", default="tatamispaces", help="Niche ID")
    parser.add_argument("--dry-run", action="store_true", help="Evaluate only, no actions")
    parser.add_argument("--max-likes", type=int, default=MAX_LIKES_PER_RUN)
    parser.add_argument("--max-replies", type=int, default=MAX_REPLIES_PER_RUN)
    parser.add_argument("--max-follows", type=int, default=MAX_FOLLOWS_PER_RUN)
    args = parser.parse_args()

    niche_id = args.niche
    dry_run = args.dry_run
    max_likes = args.max_likes
    max_replies = args.max_replies
    max_follows = args.max_follows

    niche = get_niche(niche_id)
    set_bsky_niche(niche_id)
    limits = _get_limits(niche_id)
    queries = _get_search_queries(niche_id)

    if not queries:
        log.error(f"No search queries for niche '{niche_id}'")
        return

    log.info(f"Bluesky engage for {niche['handle']} ({'DRY RUN' if dry_run else 'LIVE'})")

    start_time = time.time()

    def time_left() -> float:
        return MAX_RUNTIME_SECONDS - (time.time() - start_time)

    # Load engagement log
    log_path = BASE_DIR / "data" / f"bluesky-engagement-log-{niche_id}.json"
    engagement_log = load_json(log_path)
    if not isinstance(engagement_log, list):
        engagement_log = []

    # Our handle for skip-self
    bsky_env = niche.get("bluesky_env", {})
    our_handle = os.environ.get(bsky_env.get("handle", ""), "").lower()

    # Select queries — weighted by past engagement, with exploration slot
    max_queries = min(load_config().get("max_queries_per_run", 5), len(queries))
    insights_path = BASE_DIR / "data" / f"insights-{niche_id}.json"
    query_perf = load_json(insights_path, default={}).get("query_performance", {})

    if query_perf and max_queries >= 2:
        weights = {}
        for q in queries:
            # Try matching cleaned version against X query perf data
            stats = query_perf.get(q)
            if stats and stats.get("posts_engaged", 0) >= 3:
                w = (stats.get("reply_engagement_rate", 0) * 3) + (stats.get("avg_score", 0) / 10) + 0.5
                weights[q] = max(w, 0.3)
            else:
                weights[q] = 0.3
        weighted_queries = list(weights.keys())
        w_values = [weights[q] for q in weighted_queries]
        n_weighted = max_queries - 1
        selected = set()
        if n_weighted > 0:
            chosen = random.choices(weighted_queries, weights=w_values, k=n_weighted * 3)
            for q in chosen:
                selected.add(q)
                if len(selected) >= n_weighted:
                    break
        remaining = [q for q in queries if q not in selected]
        if remaining:
            selected.add(random.choice(remaining))
        shuffled_queries = list(selected)[:max_queries]
    else:
        shuffled_queries = random.sample(queries, max_queries)

    log.info(f"Using {len(shuffled_queries)} queries")

    # Search and collect posts
    all_posts: list[BskyPost] = []
    seen_uris = set()
    min_likes = niche.get("engagement", {}).get("min_likes", 1)

    # Use lower min_likes for Bluesky (smaller community)
    bsky_min_likes = max(1, min_likes // 3)

    failed_searches = 0
    for i, query in enumerate(shuffled_queries):
        if time_left() < 120:
            log.warning(f"Time budget low ({time_left():.0f}s), stopping searches")
            break
        if rate_budget_remaining() < 50:
            log.warning("Rate budget low, stopping searches")
            break

        log.info(f"Searching: {query[:60]}...")
        posts = search_posts(query, limit=POSTS_PER_QUERY)

        if not posts:
            failed_searches += 1
            log.warning(f"Search returned 0 results ({failed_searches} failures)")
            if failed_searches >= 3:
                log.warning("3+ search failures, stopping")
                break
            time.sleep(3)
            continue

        before = len(posts)
        posts = [p for p in posts if p.likes >= bsky_min_likes]
        log.info(f"  {len(posts)}/{before} posts with >= {bsky_min_likes} likes")

        for p in posts:
            if p.uri not in seen_uris:
                seen_uris.add(p.uri)
                p._source_query = query
                all_posts.append(p)

        if i < len(shuffled_queries) - 1:
            time.sleep(random.uniform(2, 5))

    log.info(f"Found {len(all_posts)} unique posts across {len(shuffled_queries)} queries")

    # Evaluate posts
    random.shuffle(all_posts)
    scored_posts = []
    eval_count = 0

    for post in all_posts:
        if time_left() < 300:
            log.warning(f"Time budget low ({time_left():.0f}s), stopping evaluations")
            break
        if already_liked(engagement_log, post.uri) and already_replied(engagement_log, post.uri):
            continue
        if post.author_handle.lower() == our_handle:
            continue

        eval_count += 1

        # Try cheap CLI eval first, fall back to API
        evaluation = _try_claude_eval(post, niche_id)
        if evaluation is None:
            evaluation = await evaluate_post(
                post_text=post.text,
                author=post.author_handle,
                niche_id=niche_id,
                image_count=post.image_count,
                likes=post.likes,
                reposts=post.reposts,
            )

        scored_posts.append((post, evaluation))
        log.info(
            f"  @{post.author_handle} — score {evaluation['relevance_score']}/10 "
            f"({evaluation['reason'][:50]})"
        )

    # Sort by score with recency boost
    def _sort_key(item):
        post, eval_data = item
        score = eval_data["relevance_score"]
        age = _post_age_minutes(post)
        if age < 30:
            score += 2
        elif age < 60:
            score += 1
        return (score, post.likes)
    scored_posts.sort(key=_sort_key, reverse=True)

    # --- Auto-like posts scoring 6+ ---
    likes_done = 0
    daily_likes = count_today_actions(engagement_log, "like")
    daily_max_likes = limits["daily_max_likes"]
    for post, eval_data in scored_posts:
        if likes_done >= max_likes:
            break
        if daily_likes + likes_done >= daily_max_likes:
            log.info(f"Daily like cap reached ({daily_max_likes})")
            break
        if time_left() < 60:
            break
        if eval_data["relevance_score"] < 6:
            continue
        if already_liked(engagement_log, post.uri):
            continue

        if dry_run:
            log.info(f"[DRY] Would like @{post.author_handle} (score {eval_data['relevance_score']})")
            likes_done += 1
        else:
            time.sleep(random.uniform(*limits["like_delay"]))
            success = like_post(post.uri, post.cid)
            if success:
                likes_done += 1
                engagement_log.append({
                    "action": "like",
                    "post_uri": post.uri,
                    "author": post.author_handle,
                    "author_did": post.author_did,
                    "score": eval_data["relevance_score"],
                    "post_likes": post.likes,
                    "author_followers": post.author_followers,
                    "query": getattr(post, '_source_query', None),
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "platform": "bluesky",
                })
                log.info(f"Liked @{post.author_handle} ({likes_done}/{max_likes})")

    # --- Reply to top posts ---
    replies_done = 0
    daily_replies = count_today_actions(engagement_log, "reply")
    daily_max_replies = limits["daily_max_replies"]
    min_followers = limits["min_author_followers_for_reply"]
    min_post_likes = limits["min_post_likes_for_reply"]
    replied_authors = set()

    for post, eval_data in scored_posts:
        if replies_done >= max_replies:
            break
        if daily_replies + replies_done >= daily_max_replies:
            log.info(f"Daily reply cap reached ({daily_max_replies})")
            break
        if time_left() < 120:
            break
        if eval_data["relevance_score"] < 8:
            continue
        if already_replied(engagement_log, post.uri):
            continue
        if post.author_handle in replied_authors:
            continue
        if replies_to_author_this_week(engagement_log, post.author_handle) >= 2:
            continue

        # Fetch follower count if not cached
        if post.author_followers == 0:
            profile = get_profile(post.author_did)
            if profile:
                post.author_followers = profile.get("followers_count", 0)

        if post.author_followers < min_followers:
            log.info(f"  Skip @{post.author_handle} — {post.author_followers} followers (need {min_followers}+)")
            continue
        if post.likes < min_post_likes:
            continue

        log.info(f"Drafting reply to @{post.author_handle} ({post.author_followers} followers, {post.likes} likes)...")
        reply_text = await draft_reply(
            post_text=post.text,
            author=post.author_handle,
            niche_id=niche_id,
        )

        if reply_text:
            if dry_run:
                log.info(f"[DRY] Would reply to @{post.author_handle}: {reply_text[:80]}...")
                replies_done += 1
                replied_authors.add(post.author_handle)
            else:
                time.sleep(random.uniform(*limits["reply_delay"]))
                # For replies, parent and root are the same (top-level reply)
                reply_uri = reply_to_post(
                    parent_uri=post.uri, parent_cid=post.cid,
                    root_uri=post.uri, root_cid=post.cid,
                    text=reply_text,
                )
                if reply_uri:
                    replies_done += 1
                    replied_authors.add(post.author_handle)
                    engagement_log.append({
                        "action": "reply",
                        "post_uri": post.uri,
                        "reply_uri": reply_uri,
                        "author": post.author_handle,
                        "author_did": post.author_did,
                        "reply_text": reply_text,
                        "score": eval_data["relevance_score"],
                        "post_likes": post.likes,
                        "author_followers": post.author_followers,
                        "query": getattr(post, '_source_query', None),
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "platform": "bluesky",
                    })
                    log.info(f"Replied to @{post.author_handle} ({replies_done}/{max_replies})")

    # --- Follow relevant accounts ---
    followed_handles = {
        e["author"] for e in engagement_log if e.get("action") == "follow"
    }
    follows_done = 0
    daily_follows = count_today_actions(engagement_log, "follow")
    daily_max_follows = limits["daily_max_follows"]
    seen_follow_handles = set()

    for post, eval_data in scored_posts:
        if follows_done >= max_follows:
            break
        if daily_follows + follows_done >= daily_max_follows:
            log.info(f"Daily follow cap reached ({daily_max_follows})")
            break
        if time_left() < 30:
            break
        if eval_data["relevance_score"] < 7:
            continue
        if post.author_handle in followed_handles or post.author_handle in seen_follow_handles:
            continue

        seen_follow_handles.add(post.author_handle)
        if dry_run:
            log.info(f"[DRY] Would follow @{post.author_handle}")
            follows_done += 1
        else:
            time.sleep(random.uniform(*limits["follow_delay"]))
            success = follow_user(post.author_did)
            if success:
                follows_done += 1
                followed_handles.add(post.author_handle)
                engagement_log.append({
                    "action": "follow",
                    "post_uri": post.uri,
                    "author": post.author_handle,
                    "author_did": post.author_did,
                    "score": eval_data["relevance_score"],
                    "post_likes": post.likes,
                    "author_followers": post.author_followers,
                    "query": getattr(post, '_source_query', None),
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "platform": "bluesky",
                })
                log.info(f"Followed @{post.author_handle} ({follows_done}/{max_follows})")

    # Save engagement log
    log_path.parent.mkdir(parents=True, exist_ok=True)
    save_json(log_path, engagement_log)

    # Summary
    elapsed = int(time.time() - start_time)
    summary = f"Done in {elapsed}s. Likes: {likes_done}, Replies: {replies_done}, Follows: {follows_done}"
    log.info(summary)
    notify(f"{niche['handle']} bsky engage", summary)


if __name__ == "__main__":
    lock_fd = acquire_lock(BASE_DIR / ".bluesky_engage.lock")
    if not lock_fd:
        log.info("Another bluesky_engage is running, exiting")
        sys.exit(0)
    try:
        asyncio.run(main())
    finally:
        release_lock(lock_fd)
