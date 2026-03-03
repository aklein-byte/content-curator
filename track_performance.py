#!/usr/bin/env python3
"""
Track performance of posted tweets via X API v2.
Stores likes, reposts, replies, views back into posts files.

Usage: python track_performance.py [--niche tatamispaces]
"""

import sys
import argparse
from pathlib import Path
from datetime import datetime, timezone, timedelta

sys.path.insert(0, str(Path(__file__).parent))

from dotenv import load_dotenv
load_dotenv()

import requests
from tools.common import load_json, save_json, setup_logging
from tools.db import acquire_process_lock, release_process_lock
from tools.xapi import _get_auth, API_BASE
from config.niches import get_niche

log = setup_logging("track_performance")
BASE_DIR = Path(__file__).parent


def _posts_path(niche_id):
    niche = get_niche(niche_id)
    return BASE_DIR / niche.get("posts_file", "posts.json")


def _lookup_tweets(tweet_ids: list[str]) -> dict:
    """Batch lookup tweet metrics. Returns {tweet_id: metrics_dict}."""
    if not tweet_ids:
        return {}
    results = {}
    # X API v2 allows up to 100 IDs per request
    for i in range(0, len(tweet_ids), 100):
        batch = tweet_ids[i:i+100]
        r = requests.get(
            f"{API_BASE}/tweets",
            params={
                "ids": ",".join(batch),
                "tweet.fields": "public_metrics,created_at",
            },
            auth=_get_auth(),
            timeout=15,
        )
        if r.status_code == 429:
            log.warning("Rate limited on tweet lookup")
            break
        if r.status_code != 200:
            log.error(f"Tweet lookup failed: {r.status_code} {r.text[:200]}")
            continue
        for tweet in r.json().get("data", []):
            metrics = tweet.get("public_metrics", {})
            results[tweet["id"]] = {
                "likes": metrics.get("like_count", 0),
                "replies": metrics.get("reply_count", 0),
                "reposts": metrics.get("retweet_count", 0),
                "quotes": metrics.get("quote_count", 0),
                "views": metrics.get("impression_count", 0),
            }
    return results


def track(niche_id: str):
    path = _posts_path(niche_id)
    lock_name = f"track_perf_{niche_id}"
    if not acquire_process_lock(lock_name):
        log.info(f"Another track_performance is running for {niche_id}, skipping")
        return
    try:
        data = load_json(path, default={"posts": []})
        posts = data.get("posts", [])

        # Find posted tweets that need tracking
        now = datetime.now(timezone.utc)
        to_check = []
        for p in posts:
            if p.get("status") != "posted":
                continue
            tid = p.get("tweet_id") or p.get("posted_tweet_id")
            if not tid:
                continue
            perf = p.get("performance", {})
            last_measured = perf.get("measured_at")
            if last_measured:
                last_dt = datetime.fromisoformat(last_measured)
                age = now - last_dt
                # Re-measure: after 6h, 24h, 3d, 7d
                if age < timedelta(hours=6):
                    continue
                measurements = perf.get("measurements", [])
                if len(measurements) >= 5:
                    continue  # Enough data points
            to_check.append((p, str(tid)))

        if not to_check:
            log.info(f"{niche_id}: No posts need performance tracking")
            return

        log.info(f"{niche_id}: Checking {len(to_check)} posts")
        tweet_ids = [tid for _, tid in to_check]
        metrics = _lookup_tweets(tweet_ids)

        updated = 0
        for p, tid in to_check:
            if tid not in metrics:
                continue
            m = metrics[tid]
            perf = p.get("performance", {})
            perf.update(m)
            perf["measured_at"] = now.isoformat()
            measurements = perf.get("measurements", [])
            measurements.append({"at": now.isoformat(), **m})
            if len(measurements) > 5:
                measurements = measurements[-5:]
            perf["measurements"] = measurements
            p["performance"] = perf
            updated += 1

        if updated:
            save_json(path, data, lock=True)
            log.info(f"{niche_id}: Updated {updated} posts with performance data")
        else:
            log.info(f"{niche_id}: No new metrics found (API may not return impression data on free tier)")

    finally:
        release_process_lock(lock_name)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--niche", default=None)
    args = parser.parse_args()

    if args.niche:
        track(args.niche)
    else:
        track("tatamispaces")
        track("museumstories")

    log.info("Done")


if __name__ == "__main__":
    main()
