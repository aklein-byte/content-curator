"""
Thread posting script for @tatamispaces.
Generates educational threads about Japanese design topics and posts them.

Usage: python thread.py [--niche tatamispaces] [--topic "topic"] [--dry-run]
"""

import sys
import os
import json
import asyncio
import random
import argparse
from pathlib import Path
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).parent))

from dotenv import load_dotenv
load_dotenv()

from tools.xapi import post_thread, set_niche as set_xapi_niche
from tools.common import load_json, save_json, notify, acquire_lock, release_lock, setup_logging
from agents.engager import generate_thread
from config.niches import get_niche

log = setup_logging("thread")

BASE_DIR = Path(__file__).parent
THREAD_LOG = Path(os.environ.get("THREAD_LOG", str(BASE_DIR / "thread-log.json")))

# Topics pool — rotate through these weekly
TOPICS = [
    "How tatami mats are made — from rice straw core to igusa grass surface",
    "The engawa: Japan's in-between space that's neither inside nor outside",
    "Shoji screens — how paper and wood control light in Japanese homes",
    "Wabi-sabi in practice: what intentional imperfection looks like in Japanese rooms",
    "The tokonoma alcove — the spiritual center of a traditional Japanese room",
    "How Japanese carpenters join wood without nails (tsugite joinery)",
    "The genkan: why Japanese homes have a sunken entryway",
    "Machiya townhouses — Kyoto's narrow wooden houses and how they survive",
    "Shou sugi ban: the Japanese technique of charring wood to preserve it",
    "Fusuma sliding doors — how rooms transform from one space to four",
    "The Japanese bathroom: ofuro tubs, separate wet rooms, and ritual bathing",
    "Irori hearths — the sunken fireplace at the center of old Japanese farmhouses",
    "How light works in Japanese architecture — from Tanizaki's In Praise of Shadows",
    "Kominka renovation — turning 100-year-old farmhouses into modern homes",
    "The roji (tea garden path) — how a short walk prepares you for tea ceremony",
]


def load_thread_log() -> list:
    return load_json(THREAD_LOG, default=[])


def save_thread_log(data: list):
    save_json(THREAD_LOG, data)


def pick_topic(thread_log: list) -> str:
    """Pick a topic we haven't done recently."""
    used_topics = {e.get("topic", "") for e in thread_log}
    unused = [t for t in TOPICS if t not in used_topics]
    if not unused:
        # All used — pick least recently used
        return random.choice(TOPICS)
    return random.choice(unused)




async def main():
    parser = argparse.ArgumentParser(description="Generate and post a thread")
    parser.add_argument("--niche", default="tatamispaces", help="Niche ID")
    parser.add_argument("--topic", default=None, help="Specific topic (otherwise picks from pool)")
    parser.add_argument("--dry-run", action="store_true", help="Generate only, don't post")
    args = parser.parse_args()

    niche_id = args.niche
    niche = get_niche(niche_id)
    thread_log = load_thread_log()

    # Pick topic
    topic = args.topic or pick_topic(thread_log)
    log.info(f"Generating thread for {niche['handle']}: {topic}")

    # Generate thread with Claude
    thread_data = await generate_thread(topic=topic, niche_id=niche_id)
    tweets = thread_data.get("tweets", [])

    if not tweets:
        log.error("Failed to generate thread — no tweets returned")
        return

    log.info(f"Generated {len(tweets)} tweets:")
    for i, t in enumerate(tweets, 1):
        log.info(f"  {i}. [{len(t)} chars] {t[:80]}...")

    if args.dry_run:
        print()
        print("=" * 60)
        print(f"DRY RUN — Thread: {topic}")
        print("=" * 60)
        for i, t in enumerate(tweets, 1):
            print(f"\n--- Tweet {i} ({len(t)} chars) ---")
            print(t)
        print("=" * 60)
        return

    # Post via X API v2
    set_xapi_niche(niche_id)
    log.info("Posting thread via X API...")

    community_id = niche.get("community_id")
    thread_data = [{"text": t} for t in tweets]
    posted_ids = post_thread(
        tweets=thread_data,
        community_id=community_id,
    )

    if posted_ids:
        thread_log.append({
            "topic": topic,
            "tweet_count": len(tweets),
            "posted_count": len(posted_ids),
            "first_tweet_id": posted_ids[0],
            "tweet_ids": posted_ids,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })
        save_thread_log(thread_log)

        thread_url = f"https://x.com/{niche['handle'].lstrip('@')}/status/{posted_ids[0]}"
        log.info(f"Thread posted: {thread_url} ({len(posted_ids)} tweets)")
        notify(f"{niche['handle']} thread", f"Posted {len(posted_ids)}-tweet thread: {topic[:40]}")
    else:
        log.error("Failed to post thread")
        notify(f"{niche['handle']} thread FAILED", f"Thread failed: {topic[:40]}")


if __name__ == "__main__":
    lock_fd = acquire_lock(BASE_DIR / ".thread.lock")
    if not lock_fd:
        log.info("Another thread.py is already running, exiting")
        sys.exit(0)
    try:
        asyncio.run(main())
    finally:
        release_lock(lock_fd)
