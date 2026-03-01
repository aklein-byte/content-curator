#!/usr/bin/env python3
"""
Track performance of Bluesky posts.
Reads posts with bluesky_post_uri, fetches current metrics,
stores in bsky_performance sub-dict (separate from X performance).

Usage: python bluesky_track.py [--niche tatamispaces]
"""

import sys
import argparse
from pathlib import Path
from datetime import datetime, timezone, timedelta

sys.path.insert(0, str(Path(__file__).parent))

from dotenv import load_dotenv
load_dotenv()

from tools.bluesky import (
    get_post_thread, set_niche as set_bsky_niche,
)
from tools.common import load_json, save_json, setup_logging, acquire_lock, release_lock
from config.niches import get_niche

log = setup_logging("bluesky_track")
BASE_DIR = Path(__file__).parent


def _posts_path(niche_id):
    niche = get_niche(niche_id)
    return BASE_DIR / niche.get("posts_file", "posts.json")


def _lookup_bsky_posts(uris: list[str]) -> dict:
    """Fetch metrics for Bluesky posts. Returns {uri: metrics_dict}."""
    results = {}
    for uri in uris:
        thread = get_post_thread(uri, depth=0)
        if thread:
            results[uri] = {
                "likes": thread.get("likes", 0),
                "replies": thread.get("replies", 0),
                "reposts": thread.get("reposts", 0),
            }
    return results


def track(niche_id: str):
    set_bsky_niche(niche_id)
    path = _posts_path(niche_id)
    lock = acquire_lock(BASE_DIR / f".bsky_track_{niche_id}.lock")
    try:
        data = load_json(path, default={"posts": []})
        posts = data.get("posts", [])

        now = datetime.now(timezone.utc)
        to_check = []
        for p in posts:
            if p.get("status") != "posted":
                continue
            uri = p.get("bluesky_post_uri")
            if not uri:
                continue
            perf = p.get("bsky_performance", {})
            last_measured = perf.get("measured_at")
            if last_measured:
                last_dt = datetime.fromisoformat(last_measured)
                age = now - last_dt
                if age < timedelta(hours=6):
                    continue
                measurements = perf.get("measurements", [])
                if len(measurements) >= 5:
                    continue
            to_check.append((p, uri))

        if not to_check:
            log.info(f"{niche_id}: No Bluesky posts need tracking")
            return

        log.info(f"{niche_id}: Checking {len(to_check)} Bluesky posts")
        uris = [uri for _, uri in to_check]
        metrics = _lookup_bsky_posts(uris)

        updated = 0
        for p, uri in to_check:
            if uri not in metrics:
                continue
            m = metrics[uri]
            perf = p.get("bsky_performance", {})
            perf.update(m)
            perf["measured_at"] = now.isoformat()
            measurements = perf.get("measurements", [])
            measurements.append({"at": now.isoformat(), **m})
            if len(measurements) > 5:
                measurements = measurements[-5:]
            perf["measurements"] = measurements
            p["bsky_performance"] = perf
            updated += 1

        if updated:
            save_json(path, data, lock=True)
            log.info(f"{niche_id}: Updated {updated} posts with Bluesky metrics")
        else:
            log.info(f"{niche_id}: No new Bluesky metrics found")

    finally:
        release_lock(lock)


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
