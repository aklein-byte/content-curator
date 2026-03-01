#!/usr/bin/env python3
"""
Quote Tweet Draft Generator for @tatamispaces.

Searches for high-engagement tweets in the Japanese design/architecture niche,
uses Claude to evaluate and draft quote tweet commentary, adds them to posts.json
as drafts for owner approval via dashboard.

Usage: python quote_drafts.py [--niche tatamispaces] [--dry-run] [--max-drafts 4]
"""

import sys
import os
import json
import logging
import argparse
import random
from pathlib import Path
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).parent))

from dotenv import load_dotenv
load_dotenv()

from tools.xapi import search_posts, _get_user_id
from tools.common import get_model, load_json, save_json, setup_logging, load_config
from config.niches import get_niche

log = setup_logging("quote_drafts")

BASE_DIR = Path(__file__).parent
POSTS_FILE = Path(os.environ.get("POSTS_FILE", str(BASE_DIR / "posts.json")))


def _resolve_posts_file(niche_id):
    """Get the posts file path for a niche."""
    niche = get_niche(niche_id)
    return BASE_DIR / niche.get("posts_file", "posts.json")

# Queries tuned for finding quotable content (high engagement, image-based)
QT_SEARCH_QUERIES = [
    # English architecture/design accounts with strong engagement
    "japanese architecture has:images -is:retweet min_faves:50",
    "japanese interior design has:images -is:retweet min_faves:30",
    "japanese house has:images -is:retweet min_faves:40",
    "tatami has:images -is:retweet min_faves:20",
    "ryokan has:images -is:retweet min_faves:30",
    "wabi sabi has:images -is:retweet min_faves:30",
    # Architecture firms & publications (broader, will filter by engagement)
    "kengo kuma has:images -is:retweet min_faves:100",
    "tadao ando has:images -is:retweet min_faves:100",
    "shigeru ban has:images -is:retweet min_faves:50",
    # Japanese-language high engagement
    "(建築 OR 設計) has:images -is:retweet min_faves:50 lang:ja",
    "(古民家 OR 町家 OR 旅館) has:images -is:retweet min_faves:30 lang:ja",
]

# Fallback queries without min_faves (API v2 may not support it)
QT_SEARCH_QUERIES_FALLBACK = [
    "japanese architecture has:images -is:retweet",
    "japanese interior design has:images -is:retweet",
    "ryokan interior has:images -is:retweet",
    "kengo kuma has:images -is:retweet",
    "shigeru ban has:images -is:retweet",
    "tatami room has:images -is:retweet",
    "(建築 OR 古民家) has:images -is:retweet lang:ja",
]

MIN_LIKES_FOR_QT = 20  # Only quote tweets with decent engagement
MIN_VIEWS_FOR_QT = 500


def load_posts(niche_id=None) -> dict:
    path = _resolve_posts_file(niche_id) if niche_id else POSTS_FILE
    return load_json(path, default={"posts": []})


def save_posts(data: dict, niche_id=None):
    path = _resolve_posts_file(niche_id) if niche_id else POSTS_FILE
    save_json(path, data)


def _get_existing_qt_ids(posts_data: dict) -> set:
    """Get tweet IDs we've already quoted, drafted, or dropped."""
    ids = set()
    for p in posts_data.get("posts", []):
        qt_id = p.get("quote_tweet_id")
        if qt_id:
            ids.add(str(qt_id))
    return ids


def _get_queued_qt_authors(posts_data: dict) -> set:
    """Get authors of QTs currently in queue (draft/approved)."""
    authors = set()
    for p in posts_data.get("posts", []):
        if p.get("type") == "quote-tweet" and p.get("status") in ("draft", "approved"):
            author = p.get("quote_tweet_author", "").lstrip("@").lower()
            if author:
                authors.add(author)
    return authors


def _get_queued_qt_summaries(posts_data: dict) -> str:
    """Get text summaries of QTs in queue for similarity check."""
    summaries = []
    for p in posts_data.get("posts", []):
        if p.get("type") == "quote-tweet" and p.get("status") in ("draft", "approved"):
            orig = p.get("quote_tweet_text", "")[:100]
            our_text = p.get("text", "")[:100]
            summaries.append(f"- @{p.get('quote_tweet_author','?')}: {orig} → Our take: {our_text}")
    return "\n".join(summaries) if summaries else "None"


def _get_next_id(posts_data: dict) -> int:
    """Get the next available post ID."""
    max_id = 0
    for p in posts_data.get("posts", []):
        pid = p.get("id", 0)
        if isinstance(pid, int) and pid > max_id:
            max_id = pid
    return max_id + 1


def _evaluate_and_draft(tweet, niche_id: str, posts_data: dict = None) -> dict | None:
    """Use Claude to evaluate if a tweet is worth quoting and draft commentary.

    Returns dict with 'text' and 'category' if worth quoting, None otherwise.
    """
    from anthropic import Anthropic

    voice_path = BASE_DIR / "config" / "voice.md"
    voice_guide = voice_path.read_text() if voice_path.exists() else ""

    niche = get_niche(niche_id)
    _cfg = load_config().get("models", {})
    model = get_model("quote_writer")

    client = Anthropic()

    queue_summaries = _get_queued_qt_summaries(posts_data) if posts_data else "None"

    categories = niche.get("qt_categories", ["ryokan", "modern-architecture", "historic-house", "temple", "residential", "craft", "adaptive-reuse", "garden", "other"])
    cat_str = ", ".join(categories)

    prompt = f"""You're evaluating a tweet for {niche['handle']} to quote-tweet.

## The tweet
Author: @{tweet.author_handle} ({tweet.author_name})
Text: {tweet.text}
Likes: {tweet.likes} | Retweets: {tweet.reposts} | Views: {tweet.views}
Has images: {len(tweet.image_urls) > 0}

## Account goals
{niche['description']}. We're a curation account building a following around this topic.
We add context audiences wouldn't know — specific details, measurements, backstory.
Quote tweets should add genuine value: a detail, a correction, context, a connection to something else, or an informed reaction.

## Already in our queue (avoid similar topics/angles)
{queue_summaries}

## Rules for the commentary
- Follow the voice guide strictly (short, specific, opinionated)
- Don't just compliment the original tweet ("Great post!" = useless)
- Add something: a fact, a detail, a context, a connection, a correction
- If you can't add real value, say SKIP
- Max 200 characters for the quote commentary
- No hashtags in quote tweets
- Don't start with "This is..." or any AI patterns
- If the original is in Japanese, your commentary should still be in English

First decide: is this tweet worth quoting? Consider:
1. Is it relevant to {niche['description']}?
2. Does it have good visuals we want our audience to see?
3. Can we add genuine value with commentary?
4. Is the author someone our audience should know about?
5. Is it too similar to something already in our queue? (SKIP if so — we want variety)

Respond in exactly this format:
VERDICT: QUOTE or SKIP
REASON: [one sentence why]
CATEGORY: [one of: {cat_str}]
TEXT: [your quote tweet commentary, max 200 chars]"""

    try:
        response = client.messages.create(
            model=model,
            max_tokens=300,
            messages=[{"role": "user", "content": prompt}],
        )

        text = response.content[0].text.strip()

        # Parse response
        verdict = ""
        reason = ""
        category = "other"
        qt_text = ""

        for line in text.split("\n"):
            line = line.strip()
            if line.startswith("VERDICT:"):
                verdict = line.replace("VERDICT:", "").strip().upper()
            elif line.startswith("REASON:"):
                reason = line.replace("REASON:", "").strip()
            elif line.startswith("CATEGORY:"):
                category = line.replace("CATEGORY:", "").strip()
            elif line.startswith("TEXT:"):
                qt_text = line.replace("TEXT:", "").strip()

        if verdict != "QUOTE" or not qt_text:
            log.info(f"  SKIP @{tweet.author_handle}: {reason}")
            return None

        log.info(f"  QUOTE @{tweet.author_handle}: {reason}")
        return {
            "text": qt_text[:280],
            "category": category,
            "reason": reason,
        }

    except Exception as e:
        log.error(f"  Claude evaluation failed: {e}")
        return None


MAX_QT_QUEUE = 4  # Skip generating if this many QTs are already waiting


def find_quotable_tweets(niche_id: str, max_drafts: int = 4, dry_run: bool = False):
    """Search for quotable tweets and create draft entries."""
    posts_data = load_posts(niche_id)

    # Skip if enough QTs already queued
    queued = sum(
        1 for p in posts_data.get("posts", [])
        if p.get("type") == "quote-tweet" and p.get("status") in ("draft", "approved")
    )
    if queued >= MAX_QT_QUEUE:
        log.info(f"Already {queued} QTs in queue (draft/approved). Skipping.")
        return 0

    existing_qt_ids = _get_existing_qt_ids(posts_data)
    our_user_id = _get_user_id()

    log.info(f"Existing QT IDs: {len(existing_qt_ids)}, queued: {queued}")
    log.info(f"Looking for up to {max_drafts} new quote tweet drafts")

    # Pick queries from niche config if available, else use defaults
    niche_cfg = get_niche(niche_id)
    niche_queries = niche_cfg.get("qt_queries", [])
    query_pool = niche_queries if niche_queries else QT_SEARCH_QUERIES_FALLBACK
    queries = random.sample(query_pool, min(3, len(query_pool)))

    queued_authors = _get_queued_qt_authors(posts_data)
    log.info(f"Authors already in queue: {queued_authors or 'none'}")

    candidates = []
    seen_ids = set()

    for query in queries:
        log.info(f"Searching: {query[:60]}...")
        results = search_posts(query, max_results=15)

        for tweet in results:
            # Skip our own tweets
            if tweet.author_id == our_user_id:
                continue
            # Skip already quoted
            if tweet.post_id in existing_qt_ids:
                continue
            # Skip duplicates within this run
            if tweet.post_id in seen_ids:
                continue
            # Skip authors already in queue
            if tweet.author_handle.lower() in queued_authors:
                continue
            # Engagement filter
            if tweet.likes < MIN_LIKES_FOR_QT and tweet.views < MIN_VIEWS_FOR_QT:
                continue

            seen_ids.add(tweet.post_id)
            candidates.append(tweet)

        log.info(f"  Found {len(results)} tweets, {len(candidates)} candidates so far")

    if not candidates:
        log.info("No candidates found. Done.")
        return 0

    # Sort by engagement (likes + views/100) and take top candidates
    candidates.sort(key=lambda t: t.likes + (t.views or 0) / 100, reverse=True)
    candidates = candidates[:max_drafts * 2]  # Evaluate more than we need

    log.info(f"Evaluating top {len(candidates)} candidates with Claude...")

    drafts_created = 0
    next_id = _get_next_id(posts_data)

    for tweet in candidates:
        if drafts_created >= max_drafts:
            break

        log.info(f"Evaluating @{tweet.author_handle}: {tweet.text[:60]}... ({tweet.likes} likes)")

        if dry_run:
            log.info(f"  [DRY RUN] Would evaluate with Claude")
            drafts_created += 1
            continue

        result = _evaluate_and_draft(tweet, niche_id, posts_data)
        if not result:
            continue

        # Create draft entry
        draft = {
            "id": next_id,
            "type": "quote-tweet",
            "status": "draft",
            "text": result["text"],
            "category": result["category"],
            "quote_tweet_id": tweet.post_id,
            "quote_tweet_author": f"@{tweet.author_handle}",
            "quote_tweet_text": tweet.text[:200],
            "quote_tweet_likes": tweet.likes,
            "image_urls": tweet.image_urls[:4],
            "source_url": f"https://x.com/{tweet.author_handle}/status/{tweet.post_id}",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "draft_reason": result["reason"],
        }

        posts_data.get("posts", []).append(draft)
        log.info(f"  Created draft #{next_id}: {result['text'][:60]}...")
        next_id += 1
        drafts_created += 1

    if drafts_created > 0 and not dry_run:
        save_posts(posts_data, niche_id)
        log.info(f"Saved {drafts_created} new quote tweet drafts")
    else:
        log.info(f"No new drafts created")

    return drafts_created


def main():
    parser = argparse.ArgumentParser(description="Generate quote tweet drafts")
    parser.add_argument("--niche", default="tatamispaces")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--max-drafts", type=int, default=4)
    args = parser.parse_args()

    count = find_quotable_tweets(args.niche, args.max_drafts, args.dry_run)
    log.info(f"Done. New drafts created: {count}")


if __name__ == "__main__":
    main()
