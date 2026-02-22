"""
Learning pipeline — analyzes engagement data to improve targeting.

Reads engagement logs, computes query performance metrics,
and writes insights to data/insights-{niche}.json.

Run after 1+ week of engagement data with query tracking (Phase A).

Usage: python learn.py [--niche tatamispaces]
"""

import sys
import json
import argparse
from pathlib import Path
from collections import defaultdict
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).parent))

from tools.common import load_json, save_json, setup_logging, niche_log_path

log = setup_logging("learn")

BASE_DIR = Path(__file__).parent


def _analyze_query_performance(niche_id: str) -> dict:
    """Group engagement log entries by source query and compute metrics.

    Returns dict with per-query stats:
        {
            "query_performance": {
                "<query>": {
                    "posts_engaged": int,
                    "likes_sent": int,
                    "replies_sent": int,
                    "follows_sent": int,
                    "reply_engagement_rate": float,  # replies that got likes or reply-backs / total
                    "avg_score": float,
                    "avg_post_likes": float,
                    "total_reply_likes": int,
                },
                ...
            },
            "recommended_min_likes": int,
            "analyzed_at": str,
            "total_entries": int,
            "entries_with_query": int,
        }
    """
    eng_log = load_json(niche_log_path("engagement-log.json", niche_id))
    if not isinstance(eng_log, list):
        eng_log = []

    # Group by query
    by_query: dict[str, list] = defaultdict(list)
    entries_with_query = 0
    for e in eng_log:
        q = e.get("query")
        if q:
            by_query[q].append(e)
            entries_with_query += 1

    query_performance = {}
    successful_reply_target_likes = []

    for query, entries in by_query.items():
        likes = [e for e in entries if e.get("action") == "like"]
        replies = [e for e in entries if e.get("action") == "reply"]
        follows = [e for e in entries if e.get("action") == "follow"]

        scores = [e.get("score", 0) for e in entries if e.get("score")]
        post_likes_vals = [e.get("post_likes", 0) for e in entries if e.get("post_likes") is not None]

        # Reply engagement: count replies that got likes or reply-backs
        replied_with_engagement = sum(
            1 for e in replies
            if (e.get("reply_likes", 0) or 0) > 0 or (e.get("reply_replies", 0) or 0) > 0
        )
        checked_replies = sum(1 for e in replies if "reply_likes" in e)
        reply_engagement_rate = (
            replied_with_engagement / checked_replies if checked_replies > 0 else 0.0
        )

        total_reply_likes = sum(e.get("reply_likes", 0) or 0 for e in replies)

        # Track target post likes for successful replies (to compute recommended_min_likes)
        for e in replies:
            if (e.get("reply_likes", 0) or 0) > 0 or (e.get("reply_replies", 0) or 0) > 0:
                pl = e.get("post_likes")
                if pl is not None:
                    successful_reply_target_likes.append(pl)

        query_performance[query] = {
            "posts_engaged": len(entries),
            "likes_sent": len(likes),
            "replies_sent": len(replies),
            "follows_sent": len(follows),
            "reply_engagement_rate": round(reply_engagement_rate, 3),
            "avg_score": round(sum(scores) / len(scores), 1) if scores else 0,
            "avg_post_likes": round(sum(post_likes_vals) / len(post_likes_vals), 0) if post_likes_vals else 0,
            "total_reply_likes": total_reply_likes,
        }

    # Recommended min_likes: 25th percentile of post likes for successful replies
    recommended_min_likes = None
    if successful_reply_target_likes:
        sorted_likes = sorted(successful_reply_target_likes)
        idx = max(0, len(sorted_likes) // 4 - 1)
        recommended_min_likes = sorted_likes[idx]

    return {
        "query_performance": query_performance,
        "recommended_min_likes": recommended_min_likes,
        "analyzed_at": datetime.now(timezone.utc).isoformat(),
        "total_entries": len(eng_log),
        "entries_with_query": entries_with_query,
    }


def main():
    parser = argparse.ArgumentParser(description="Analyze engagement data")
    parser.add_argument("--niche", default="tatamispaces", help="Niche ID")
    args = parser.parse_args()

    niche_id = args.niche

    log.info(f"Analyzing engagement data for {niche_id}...")

    results = _analyze_query_performance(niche_id)

    qp = results["query_performance"]
    log.info(f"  Total entries: {results['total_entries']}")
    log.info(f"  Entries with query field: {results['entries_with_query']}")
    log.info(f"  Unique queries: {len(qp)}")

    if results["recommended_min_likes"] is not None:
        log.info(f"  Recommended min_likes: {results['recommended_min_likes']}")

    # Sort by reply engagement rate descending
    sorted_queries = sorted(qp.items(), key=lambda x: x[1]["reply_engagement_rate"], reverse=True)
    if sorted_queries:
        log.info(f"\n  Top queries by reply engagement rate:")
        for query, stats in sorted_queries[:10]:
            log.info(
                f"    [{stats['reply_engagement_rate']:.0%}] "
                f"{query[:60]} "
                f"(engaged={stats['posts_engaged']}, replies={stats['replies_sent']}, "
                f"reply_likes={stats['total_reply_likes']})"
            )

    # Save to insights file
    insights_path = BASE_DIR / "data" / f"insights-{niche_id}.json"
    existing = load_json(insights_path, default={})
    if not isinstance(existing, dict):
        existing = {}

    existing["query_performance"] = results["query_performance"]
    existing["recommended_min_likes"] = results["recommended_min_likes"]
    existing["last_analyzed"] = results["analyzed_at"]

    save_json(insights_path, existing)
    log.info(f"\n  Insights saved to {insights_path}")


if __name__ == "__main__":
    main()
