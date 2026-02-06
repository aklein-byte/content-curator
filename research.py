"""
Content research script for @tatamispaces.
Searches X for high-quality JP architecture posts with good images,
uses Claude to evaluate and draft captions, appends to posts.json as drafts.

Usage: python research.py [--niche tatamispaces] [--min-likes 200]
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

from tools.xkit import login, search_posts, get_user_posts, XPost
from agents.engager import evaluate_post
from agents.writer import write_caption
from config.niches import get_niche

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("research")

BASE_DIR = Path(__file__).parent
POSTS_FILE = Path(os.environ.get("POSTS_FILE", str(BASE_DIR / "posts.json")))

# Delay between searches
DELAY_MIN = 15
DELAY_MAX = 60

# How many candidate posts to evaluate per query
CANDIDATES_PER_QUERY = 10


def load_posts() -> dict:
    if POSTS_FILE.exists():
        return json.loads(POSTS_FILE.read_text())
    return {"posts": []}


def save_posts(data: dict):
    POSTS_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False, default=str))


def next_post_id(posts_data: dict) -> int:
    existing_ids = [p.get("id", 0) for p in posts_data.get("posts", [])]
    return max(existing_ids, default=0) + 1


def already_in_queue(posts_data: dict, source_url: str) -> bool:
    """Check if a source URL is already in the post queue."""
    for p in posts_data.get("posts", []):
        if p.get("source_url") and source_url in p["source_url"]:
            return True
    return False


async def random_delay(label: str = ""):
    wait = random.uniform(DELAY_MIN, DELAY_MAX)
    if label:
        log.info(f"Waiting {wait:.0f}s before {label}...")
    await asyncio.sleep(wait)


def notify(title: str, message: str):
    try:
        os.system(
            f'terminal-notifier -title "{title}" -message "{message}" '
            f'-sound default -group content-curator 2>/dev/null'
        )
    except Exception:
        pass


async def main():
    parser = argparse.ArgumentParser(description="Research content for posting")
    parser.add_argument("--niche", default="tatamispaces", help="Niche ID")
    parser.add_argument("--min-likes", type=int, default=200, help="Minimum likes to consider")
    parser.add_argument("--max-drafts", type=int, default=5, help="Max new drafts to create")
    args = parser.parse_args()

    niche_id = args.niche
    min_likes = args.min_likes
    max_drafts = args.max_drafts
    niche = get_niche(niche_id)
    engagement_cfg = niche.get("engagement", {})
    queries = engagement_cfg.get("search_queries", [])
    tracked_accounts = engagement_cfg.get("tracked_accounts", [])

    log.info(f"Researching content for {niche['handle']}")

    # Login
    client = await login(niche_id)
    log.info("Logged in successfully")

    posts_data = load_posts()

    # Gather candidate posts from search queries
    candidates: list[XPost] = []
    seen_ids = set()

    # Search queries -- pick a subset to avoid hammering
    selected_queries = random.sample(queries, min(len(queries), 3))

    for query in selected_queries:
        log.info(f"Searching: {query[:60]}...")
        posts = await search_posts(client, query, count=CANDIDATES_PER_QUERY, product="Top")
        for p in posts:
            if p.post_id not in seen_ids and len(p.image_urls) > 0 and p.likes >= min_likes:
                seen_ids.add(p.post_id)
                candidates.append(p)
        await random_delay("next search")

    # Also check tracked accounts for recent posts with images
    selected_accounts = random.sample(tracked_accounts, min(len(tracked_accounts), 2))

    for handle in selected_accounts:
        log.info(f"Checking @{handle.lstrip('@')}...")
        posts = await get_user_posts(client, handle, count=10)
        for p in posts:
            if p.post_id not in seen_ids and len(p.image_urls) > 0 and p.likes >= min(min_likes // 2, 50):
                seen_ids.add(p.post_id)
                candidates.append(p)
        await random_delay("next account")

    log.info(f"Found {len(candidates)} candidates with images and {min_likes}+ likes")

    if not candidates:
        log.info("No candidates found. Try lowering --min-likes or expanding queries.")
        return

    # Sort by likes descending -- best performing content first
    candidates.sort(key=lambda p: p.likes, reverse=True)

    # Evaluate and draft captions for top candidates
    drafts_created = 0

    for post in candidates:
        if drafts_created >= max_drafts:
            break

        source_url = f"https://x.com/{post.author_handle}/status/{post.post_id}"
        if already_in_queue(posts_data, post.post_id):
            log.info(f"  Skipping @{post.author_handle} -- already in queue")
            continue

        # Evaluate relevance
        evaluation = await evaluate_post(
            post_text=post.text,
            author=post.author_handle,
            niche_id=niche_id,
            image_count=len(post.image_urls),
            likes=post.likes,
            reposts=post.reposts,
        )

        score = evaluation["relevance_score"]
        if score < 7:
            log.info(f"  Skipping @{post.author_handle} -- score {score}/10: {evaluation['reason'][:50]}")
            continue

        log.info(f"  @{post.author_handle} -- score {score}/10, {post.likes} likes")

        # Draft a caption using the writer agent
        caption_data = await write_caption(
            niche_id=niche_id,
            image_context=post.text,
            source_name=f"@{post.author_handle}",
            curator_notes=evaluation["reason"],
        )

        caption = caption_data["caption"]
        hashtags = caption_data["hashtags"]

        # Build full post text with credit and hashtags
        full_text = caption
        if not any(f"@{post.author_handle}" in caption for _ in [1]):
            full_text += f"\n\U0001f4f7 @{post.author_handle}"
        if hashtags:
            full_text += "\n" + " ".join(hashtags[:2])

        new_post = {
            "id": next_post_id(posts_data),
            "type": "repost-with-credit",
            "text": full_text,
            "image": None,
            "image_urls": post.image_urls,
            "source_url": source_url,
            "source_handle": f"@{post.author_handle}",
            "status": "draft",
            "relevance_score": score,
            "source_likes": post.likes,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }

        posts_data["posts"].append(new_post)
        drafts_created += 1

        log.info(f"  Draft #{new_post['id']}: {full_text[:80]}...")

    # Save
    save_posts(posts_data)

    # Print summary
    print("\n" + "=" * 60)
    print(f"RESEARCH COMPLETE for {niche['handle']}")
    print(f"=" * 60)
    print(f"Candidates found:  {len(candidates)}")
    print(f"New drafts created: {drafts_created}")
    print(f"Total posts in queue: {len(posts_data['posts'])}")
    print()

    draft_posts = [p for p in posts_data["posts"] if p.get("status") == "draft"]
    if draft_posts:
        print("Drafts pending review:")
        for p in draft_posts:
            print(f"  #{p['id']} — {p['text'][:70]}...")
            print(f"          from {p.get('source_handle', '?')} | score {p.get('relevance_score', '?')}")
        print()
        print(f"Edit {POSTS_FILE} to change status from 'draft' to 'approved'")
        print("and add a 'scheduled_for' timestamp.")
    else:
        print("No new drafts. Try lowering --min-likes or running again later.")

    print()
    notify("@tatamispaces research", f"{drafts_created} new drafts ready for review")


if __name__ == "__main__":
    asyncio.run(main())
