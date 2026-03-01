#!/usr/bin/env python3
"""
Bluesky catch-up script — backfill existing X posts to Bluesky.

Runs daily (or manually). Posts up to N posts per niche per run,
oldest-first, with delays between posts. Skips posts already on Bluesky.

Usage:
    python bluesky_catchup.py                          # all niches, 10/niche
    python bluesky_catchup.py --niche tatamispaces     # single niche
    python bluesky_catchup.py --limit 5                # 5 per niche
    python bluesky_catchup.py --dry-run                # preview only
"""

import sys
import os
import re
import json
import time
import random
import argparse
import logging
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from dotenv import load_dotenv
load_dotenv()

from tools.bluesky import (
    set_niche as set_bsky_niche,
    create_post as bsky_create_post,
    post_thread as bsky_post_thread,
    count_graphemes,
)
from tools.xapi import download_image
from tools.post_queue import load_posts, save_posts
from tools.common import notify, setup_logging
from config.niches import get_niche

log = setup_logging("bluesky_catchup")

BASE_DIR = Path(__file__).parent
IMAGES_DIR = BASE_DIR / "data" / "images"

DEFAULT_LIMIT = 10  # posts per niche per run
POST_DELAY = (120, 180)  # 2-3 min between posts


def _strip_mentions(text: str | None) -> str:
    if not text:
        return ""
    return re.sub(r'@(\w+)', r'\1', text)


def _download_museum_images(tw: dict, post: dict) -> list[str]:
    """Download images for a single museum tweet."""
    image_urls = []
    if tw.get("image_url"):
        image_urls = [tw["image_url"]]
    elif tw.get("images") and post.get("allImages"):
        image_urls = [post["allImages"][idx] for idx in tw["images"] if idx < len(post["allImages"])]

    paths = []
    for url in image_urls:
        local = download_image(url, save_dir=str(IMAGES_DIR / "museum"))
        if local:
            paths.append(local)
    return paths


def _download_flat_images(post: dict) -> list[str]:
    """Download images for a flat-format post."""
    paths = []

    # Check pre-downloaded
    if post.get("image"):
        img_path = BASE_DIR / post["image"]
        if img_path.exists():
            return [str(img_path)]

    image_urls = post.get("image_urls", [])
    handle = post.get("source_handle", "unknown").lstrip("@")
    save_dir = str(BASE_DIR / "images" / handle)

    for url in image_urls:
        local = download_image(url, save_dir=save_dir)
        if local:
            paths.append(local)

    # Respect image_index/image_count selections
    img_index = post.get("image_index")
    img_count = post.get("image_count")
    if img_index is not None and 0 <= img_index < len(paths):
        paths = [paths[img_index]]
    elif img_count is not None and img_count < len(paths):
        paths = paths[:img_count]

    return paths


def catchup_niche(niche_id: str, limit: int, dry_run: bool):
    """Backfill posted X content to Bluesky for one niche."""
    niche = get_niche(niche_id)
    if not niche.get("bluesky_env"):
        log.info(f"{niche_id}: no bluesky_env, skipping")
        return 0

    # Need to set X API niche for image downloads
    from tools.xapi import set_niche as set_xapi_niche
    set_xapi_niche(niche_id)
    set_bsky_niche(niche_id)

    posts_data = load_posts(niche_id)
    posts = posts_data.get("posts", [])

    # Find posted posts without bluesky_post_uri, oldest first
    candidates = [
        p for p in posts
        if p.get("status") == "posted" and not p.get("bluesky_post_uri")
    ]
    candidates.sort(key=lambda p: p.get("posted_at", ""))

    if not candidates:
        log.info(f"{niche_id}: all posts already on Bluesky")
        return 0

    batch = candidates[:limit]
    log.info(f"{niche_id}: {len(candidates)} pending, posting {len(batch)} this run")

    posted = 0
    for i, post in enumerate(batch):
        post_id = post.get("id", "?")
        text = post.get("text") or (post.get("tweets") or [{}])[0].get("text", "")

        is_museum = post.get("type") == "museum" and post.get("tweets")
        is_museum_thread = is_museum and len(post.get("tweets", [])) > 1

        if dry_run:
            fmt = "thread" if is_museum_thread else "single"
            print(f"  [{i+1}/{len(batch)}] #{post_id} ({fmt}) {text[:70]}...")
            posted += 1
            continue

        try:
            if is_museum_thread:
                # Museum thread
                thread_posts = []
                for tw in post["tweets"]:
                    local_paths = _download_museum_images(tw, post)
                    thread_posts.append({
                        "text": _strip_mentions(tw["text"]),
                        "image_paths": local_paths,
                        "alt_texts": [],
                    })
                uris = bsky_post_thread(thread_posts)
                if uris:
                    post["bluesky_post_uri"] = uris[0]
                    post["bluesky_thread_uris"] = uris
                    log.info(f"  #{post_id}: {len(uris)}-post thread")
                    posted += 1

            elif is_museum:
                # Single museum tweet
                tw = post["tweets"][0]
                local_paths = _download_museum_images(tw, post)
                uri = bsky_create_post(
                    text=_strip_mentions(tw["text"]),
                    image_paths=local_paths,
                )
                if uri:
                    post["bluesky_post_uri"] = uri
                    log.info(f"  #{post_id}: single museum post")
                    posted += 1

            else:
                # Flat post (tatami/cosmic)
                image_paths = _download_flat_images(post)
                has_thread_captions = post.get("thread_captions") and len(post.get("thread_captions", [])) > 1

                if has_thread_captions:
                    thread_posts = []
                    for j, cap in enumerate(post["thread_captions"]):
                        img = [image_paths[j]] if j < len(image_paths) else []
                        thread_posts.append({
                            "text": _strip_mentions(cap),
                            "image_paths": img,
                            "alt_texts": [],
                        })
                    uris = bsky_post_thread(thread_posts)
                    if uris:
                        post["bluesky_post_uri"] = uris[0]
                        post["bluesky_thread_uris"] = uris
                        log.info(f"  #{post_id}: {len(uris)}-post thread")
                        posted += 1
                else:
                    uri = bsky_create_post(
                        text=_strip_mentions(post.get("text", "")),
                        image_paths=image_paths,
                    )
                    if uri:
                        post["bluesky_post_uri"] = uri
                        log.info(f"  #{post_id}: single post")
                        posted += 1

        except Exception as e:
            log.warning(f"  #{post_id}: failed — {e}")

        # Save after each post (in case of crash)
        save_posts(posts_data, niche_id, lock=True)

        # Delay between posts (skip after last)
        if i < len(batch) - 1:
            delay = random.randint(*POST_DELAY)
            log.info(f"  Waiting {delay}s...")
            time.sleep(delay)

    return posted


def main():
    parser = argparse.ArgumentParser(description="Backfill X posts to Bluesky")
    parser.add_argument("--niche", help="Single niche to catch up")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help=f"Max posts per niche (default {DEFAULT_LIMIT})")
    parser.add_argument("--dry-run", action="store_true", help="Preview only")
    args = parser.parse_args()

    niches = [args.niche] if args.niche else ["tatamispaces", "museumstories", "cosmicshots"]

    total = 0
    for niche_id in niches:
        log.info(f"\n{'='*40}\nCatch-up: {niche_id}\n{'='*40}")
        count = catchup_niche(niche_id, args.limit, args.dry_run)
        total += count
        log.info(f"{niche_id}: {count} posts {'previewed' if args.dry_run else 'posted'}")

    if total and not args.dry_run:
        notify("Bluesky catch-up", f"Posted {total} backfill posts to Bluesky")

    log.info(f"\nDone: {total} total")


if __name__ == "__main__":
    main()
