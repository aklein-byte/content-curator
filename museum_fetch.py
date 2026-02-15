"""
Museum content pipeline for @museumstories.

Fetches objects from open museum APIs, scores them for story potential,
generates post copy with Claude, and adds to the posts queue.

Three discovery strategies:
1. Narrative keywords — drama, conflict, mystery
2. Cleveland fun_facts — pre-written story hooks
3. Period rotation — cycle through historical eras

Usage:
    python museum_fetch.py [--dry-run] [--batch-size 4]
"""

import sys
import os
import json
import random
import logging
import argparse
from pathlib import Path
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from collections import Counter
from dataclasses import asdict

sys.path.insert(0, str(Path(__file__).parent))

from dotenv import load_dotenv
load_dotenv()

from tools.museum_apis import (
    MuseumObject, met_search, aic_search, cleveland_search, smk_search, search_all,
)
from tools.common import load_json, save_json, notify, acquire_lock, release_lock, setup_logging
from config.niches import get_niche

log = setup_logging("museum_fetch")

BASE_DIR = Path(__file__).parent
ET = ZoneInfo("America/New_York")

NICHE_ID = "museumstories"

# --- Discovery strategies ---

NARRATIVE_KEYWORDS = [
    # Conflict & drama
    "war", "battle", "assassination", "execution", "murder", "poison",
    "scandal", "theft", "stolen", "looted", "forgery", "destroyed",
    "siege", "duel", "revenge", "treason",
    # Human stories
    "portrait", "self-portrait", "death", "funeral", "marriage", "exile",
    "prisoner", "slave", "king", "queen", "emperor", "pharaoh",
    # Mystery & discovery
    "tomb", "treasure", "secret", "hidden", "excavated",
    "unknown artist", "lost", "fragment", "ruins", "shipwreck",
    # Craft & extreme
    "miniature", "gold", "silver", "ivory", "jade", "silk",
    "weapon", "armor", "sword", "dagger", "shield",
    "crown", "ring", "necklace", "mask",
    # Animals & nature
    "lion", "eagle", "dragon", "horse", "snake", "sphinx",
    "hippopotamus", "crocodile",
]

PERIOD_SEARCHES = [
    {"query": "ancient egypt pharaoh", "period": "ancient"},
    {"query": "ancient greece", "period": "ancient"},
    {"query": "roman empire", "period": "ancient"},
    {"query": "viking norse", "period": "medieval"},
    {"query": "medieval knight", "period": "medieval"},
    {"query": "crusade", "period": "medieval"},
    {"query": "renaissance italy", "period": "renaissance"},
    {"query": "baroque", "period": "baroque"},
    {"query": "edo japan samurai", "period": "asian"},
    {"query": "ming dynasty china", "period": "asian"},
    {"query": "mughal india", "period": "asian"},
    {"query": "aztec maya", "period": "precolumbian"},
    {"query": "african mask", "period": "african"},
    {"query": "impressionist", "period": "modern"},
    {"query": "art nouveau", "period": "modern"},
    {"query": "world war", "period": "contemporary"},
]


def fetch_candidates() -> list[MuseumObject]:
    """Run all discovery strategies. Returns 20-40 raw candidates."""
    candidates = []

    # Strategy 1: Narrative keywords (4 random keywords, all APIs)
    keywords = random.sample(NARRATIVE_KEYWORDS, k=4)
    for kw in keywords:
        # Pick 2 random APIs per keyword to avoid hammering all 4
        apis = random.sample(["met", "aic", "cleveland", "smk"], k=2)
        results = search_all(kw, limit_per_api=3, apis=apis)
        candidates.extend(results)
        log.info(f"Keyword '{kw}': {len(results)} candidates")

    # Strategy 2: Cleveland fun_facts (goldmine)
    try:
        cleveland_curated = cleveland_search("", limit=50, require_fun_fact=True)
        if cleveland_curated:
            # Take random sample
            sample = random.sample(cleveland_curated, k=min(8, len(cleveland_curated)))
            candidates.extend(sample)
            log.info(f"Cleveland fun_facts: {len(sample)} candidates")
    except Exception as e:
        log.warning(f"Cleveland fun_facts failed: {e}")

    # Strategy 3: Period rotation (2 random periods)
    periods = random.sample(PERIOD_SEARCHES, k=2)
    for p in periods:
        apis = random.sample(["met", "aic", "cleveland", "smk"], k=2)
        results = search_all(p["query"], limit_per_api=3, apis=apis)
        candidates.extend(results)
        log.info(f"Period '{p['query']}': {len(results)} candidates")

    # Deduplicate by object ID
    seen = set()
    unique = []
    for obj in candidates:
        if obj.id not in seen:
            seen.add(obj.id)
            unique.append(obj)

    log.info(f"Total unique candidates: {len(unique)}")
    return unique


# --- Quality scoring ---

def score_metadata_richness(obj: MuseumObject) -> float:
    """0-100 score based on how much story material we have."""
    score = 0.0

    # Core fields (40 pts)
    if obj.title and len(obj.title) > 5:
        score += 10
    if obj.artist:
        score += 10
    if obj.date:
        score += 10
    if obj.medium:
        score += 10

    # Story fields (40 pts)
    if obj.description and len(obj.description) > 50:
        score += 15
    if obj.description and len(obj.description) > 200:
        score += 5
    if obj.fun_fact or obj.did_you_know:
        score += 15  # Cleveland goldmine
    if obj.wall_description and len(obj.wall_description) > 50:
        score += 5

    # Context fields (20 pts)
    if obj.culture:
        score += 5
    if obj.period:
        score += 5
    if obj.dimensions:
        score += 5
    if obj.tags and len(obj.tags) >= 2:
        score += 5

    return score


def score_image_quality(obj: MuseumObject) -> float:
    """0-100 score based on available images."""
    if not obj.primary_image_url:
        return 0.0

    score = 50.0  # Has primary image

    n_additional = len(obj.additional_images)
    if n_additional >= 1:
        score += 15
    if n_additional >= 2:
        score += 15
    if n_additional >= 3:
        score += 20

    return score


def score_novelty(obj: MuseumObject, post_history: list[dict]) -> float:
    """0-100 score. Penalize objects similar to recent posts."""
    score = 100.0

    recent = post_history[-30:] if post_history else []
    if not recent:
        return score

    # Exact object? Disqualify
    recent_ids = {p.get("object_id") for p in recent}
    if obj.id in recent_ids:
        return 0.0

    # Same artist in last 10? Big penalty
    last_10_artists = [p.get("artist") for p in recent[-10:] if p.get("artist")]
    if obj.artist and obj.artist in last_10_artists:
        score -= 40

    # Same museum in last 5? Penalize
    last_5_museums = [p.get("museum") for p in recent[-5:]]
    museum_count = last_5_museums.count(obj.museum)
    score -= museum_count * 10

    # Same culture in last 10? Minor penalty
    last_10_cultures = [p.get("culture") for p in recent[-10:] if p.get("culture")]
    if obj.culture and obj.culture in last_10_cultures:
        score -= 15

    return max(score, 0.0)


def apply_diversity_boost(candidates: list[MuseumObject], post_history: list[dict]) -> list[MuseumObject]:
    """Boost scores for underrepresented categories."""
    recent = post_history[-30:] if post_history else []
    if not recent:
        return candidates

    museum_counts = Counter(p.get("museum") for p in recent)
    total = len(recent)

    for obj in candidates:
        # Boost underrepresented museums
        freq = museum_counts.get(obj.museum, 0) / total if total else 0
        if freq < 0.15:
            obj._diversity_boost = 1.3
        elif freq > 0.4:
            obj._diversity_boost = 0.8
        else:
            obj._diversity_boost = 1.0

        # Boost non-paintings (they're overrepresented in most collections)
        if obj.medium and "oil on canvas" not in (obj.medium or "").lower():
            obj._diversity_boost *= 1.1

    return candidates


def filter_and_rank(candidates: list[MuseumObject], post_history: list[dict], min_score: float = 40.0) -> list[MuseumObject]:
    """Score, filter, and rank candidates. Returns top 10."""
    for obj in candidates:
        meta = score_metadata_richness(obj)
        img = score_image_quality(obj)
        novelty = score_novelty(obj, post_history)

        obj._meta_score = meta
        obj._img_score = img
        obj._novelty_score = novelty
        obj._total_score = meta * 0.5 + img * 0.3 + novelty * 0.2

    # Apply diversity boost
    candidates = apply_diversity_boost(candidates, post_history)
    for obj in candidates:
        boost = getattr(obj, '_diversity_boost', 1.0)
        obj._total_score *= boost

    # Filter and sort
    qualified = [obj for obj in candidates if obj._total_score >= min_score]
    qualified.sort(key=lambda x: x._total_score, reverse=True)

    if qualified:
        log.info(f"Top 5 candidates:")
        for obj in qualified[:5]:
            log.info(f"  [{obj._total_score:.0f}] {obj.museum}: {obj.title[:60]} (meta={obj._meta_score:.0f} img={obj._img_score:.0f} nov={obj._novelty_score:.0f})")

    return qualified[:10]


# --- Format decision ---

def decide_format(obj: MuseumObject) -> str:
    """Decide single tweet vs thread based on available material."""
    image_count = 1 + len(obj.additional_images)

    has_rich_story = (
        (obj.description and len(obj.description) > 300) or
        (obj.fun_fact and obj.did_you_know) or
        (obj.wall_description and len(obj.wall_description) > 200)
    )

    if image_count >= 3 and has_rich_story:
        return "thread"

    return "single"


# --- Story generation ---

def _load_voice_guide() -> str:
    voice_path = BASE_DIR / "config" / "voice-museumstories.md"
    if voice_path.exists():
        return voice_path.read_text()
    return ""


def generate_story(obj: MuseumObject, fmt: str) -> dict | None:
    """Call Claude to generate post copy for a museum object.

    Returns:
        {
            "tweets": [{"text": "...", "image_urls": ["..."]}],
            "metadata": {"object_id": "...", "museum": "...", ...}
        }
    """
    from anthropic import Anthropic

    client = Anthropic()
    niche = get_niche(NICHE_ID)
    voice_guide = _load_voice_guide()

    # Build image list
    images = [obj.primary_image_url] + obj.additional_images[:4]
    image_list = "\n".join(f"{i+1}. {url}" for i, url in enumerate(images))

    # Build metadata block
    meta_parts = [
        f"Museum: {obj.museum.upper()} ({'Metropolitan Museum of Art' if obj.museum == 'met' else 'Art Institute of Chicago' if obj.museum == 'aic' else 'Cleveland Museum of Art' if obj.museum == 'cleveland' else 'SMK Denmark'})",
        f"Title: {obj.title}",
        f"Artist: {obj.artist or 'Unknown'}",
        f"Date: {obj.date or 'Unknown'}",
    ]
    if obj.medium:
        meta_parts.append(f"Medium: {obj.medium}")
    if obj.dimensions:
        meta_parts.append(f"Dimensions: {obj.dimensions}")
    if obj.culture:
        meta_parts.append(f"Culture/Origin: {obj.culture}")
    if obj.period:
        meta_parts.append(f"Period: {obj.period}")
    if obj.department:
        meta_parts.append(f"Department: {obj.department}")
    if obj.classification:
        meta_parts.append(f"Classification: {obj.classification}")
    if obj.description:
        meta_parts.append(f"\nDescription: {obj.description[:1000]}")
    if obj.fun_fact:
        meta_parts.append(f"\nFun fact: {obj.fun_fact}")
    if obj.did_you_know:
        meta_parts.append(f"\nDid you know: {obj.did_you_know}")
    if obj.wall_description:
        meta_parts.append(f"\nWall text: {obj.wall_description[:500]}")
    if obj.tags:
        meta_parts.append(f"\nTags: {', '.join(obj.tags[:10])}")
    meta_parts.append(f"\nObject URL: {obj.object_url}")

    metadata_block = "\n".join(meta_parts)

    if fmt == "single":
        format_instructions = """Write a SINGLE TWEET. This is a Premium account, no 280 char limit.
Write as long as the story needs. 300-500 chars is typical. Let the story breathe.
Include one or more image URLs from the available images.
Must end with a signature line: "Artist, Title, Year. Museum."
Do NOT use em-dashes (—). Use periods or commas instead."""
    else:
        format_instructions = """Write a THREAD of 2-3 tweets. Premium account, no 280 char limit.
Keep each tweet under 500 chars for readability. Threads should be punchy.
CRITICAL: Each tweet MUST use a DIFFERENT image. NEVER repeat the same image URL across tweets.
If you only have crops of the same photo, write a single tweet instead.
If you reference another artwork or comparison, you MUST include it as an image. Don't mention things you can't show.
The LAST tweet must end with: "Artist, Title, Year. Museum."
Don't pad. Every tweet must advance the story. 2 strong tweets > 3 weak ones.
Do NOT use em-dashes (—). Use periods or commas instead."""

    prompt = f"""## OBJECT METADATA
{metadata_block}

## AVAILABLE IMAGES ({len(images)} total)
{image_list}

## FORMAT
{format_instructions}

## INSTRUCTIONS

1. HOOK FIRST. Don't start with "This is [title]." Start with what makes this object interesting.
2. Include context the viewer can't see: backstory, technique, scandal, how it got to the museum.
3. Specific details: dates, dimensions, materials, names, costs.
4. End with the fact that sticks.
5. Explain anything a layperson wouldn't know (but don't lecture).
6. If you reference another work, it must be in an image. Don't mention things you can't show.

## OUTPUT
Return valid JSON only:

{{
  "tweets": [
    {{"text": "Tweet text here", "image_url": "https://..." or null}}
  ]
}}

No markdown, no explanation, just the JSON."""

    system = f"""You write posts for @museumstories on X/Twitter. Compelling narrator voice.

{niche['writer_prompt']}

{voice_guide}"""

    try:
        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=1500,
            system=system,
            messages=[{"role": "user", "content": prompt}],
        )

        text = response.content[0].text.strip()

        # Parse JSON (use JSONDecoder for robustness)
        json_start = text.find("{")
        if json_start < 0:
            log.error(f"No JSON in response for {obj.title}")
            return None

        try:
            story = json.JSONDecoder().raw_decode(text, json_start)[0]
        except json.JSONDecodeError as e:
            log.error(f"JSON parse failed for {obj.title}: {e}")
            return None

        if "tweets" not in story or not story["tweets"]:
            log.error(f"No tweets in response for {obj.title}")
            return None

        # Validate: reject banned words
        banned = ["delve", "tapestry", "vibrant", "realm", "nestled", "testament", "beacon",
                  "multifaceted", "landscape", "groundbreaking", "fostering", "leveraging"]
        banned_phrases = ["not just", "more than just", "isn't merely", "a testament to",
                         "a beacon of", "at the heart of", "rich tapestry"]
        all_text = " ".join(t["text"] for t in story["tweets"])
        all_lower = all_text.lower()
        for word in banned:
            if word.lower() in all_lower:
                log.warning(f"Banned word '{word}' in generated post for {obj.title} — rejecting")
                return None
        for phrase in banned_phrases:
            if phrase in all_lower:
                log.warning(f"Banned phrase '{phrase}' in generated post for {obj.title} — rejecting")
                return None

        # Validate tweet length. Account is Premium (25k limit) so no hard 280 cap.
        # Thread tweets: keep under 600 for readability (threads should be punchy)
        # Singles: no limit (let the story breathe)
        if len(story["tweets"]) > 1:
            for i, tweet in enumerate(story["tweets"]):
                if len(tweet["text"]) > 600:
                    log.warning(f"Thread tweet {i+1} is {len(tweet['text'])} chars for {obj.title} — rejecting")
                    return None

        # Validate: no duplicate images across tweets in a thread
        if len(story["tweets"]) > 1:
            image_urls = [t.get("image_url") for t in story["tweets"] if t.get("image_url")]
            if len(image_urls) != len(set(image_urls)):
                log.warning(f"Duplicate images in thread for {obj.title} — rejecting")
                return None

        # Validate: em-dashes
        if "\u2014" in all_text or "—" in all_text:
            log.warning(f"Em-dash found in generated post for {obj.title} — rejecting")
            return None

        # Add metadata
        story["metadata"] = {
            "object_id": obj.id,
            "museum": obj.museum,
            "title": obj.title,
            "artist": obj.artist,
            "date": obj.date,
            "medium": obj.medium,
            "culture": obj.culture,
            "period": obj.period,
        }

        return story

    except Exception as e:
        log.error(f"Story generation failed for {obj.title}: {e}")
        return None


# --- Queue management ---

def get_posts_file() -> Path:
    niche = get_niche(NICHE_ID)
    return BASE_DIR / niche.get("posts_file", "posts-museumstories.json")


def load_posts() -> dict:
    return load_json(get_posts_file(), default={"posts": []})


def save_posts(data: dict):
    save_json(get_posts_file(), data)


def get_post_history(posts_data: dict) -> list[dict]:
    """Extract history for novelty scoring."""
    return [
        {
            "object_id": p.get("object_id"),
            "museum": p.get("museum"),
            "artist": p.get("artist"),
            "culture": p.get("culture"),
            "period": p.get("period"),
            "medium": p.get("medium"),
        }
        for p in posts_data.get("posts", [])
    ]


def next_post_id(posts_data: dict) -> int:
    existing = [p.get("id", 0) for p in posts_data.get("posts", [])]
    return max(existing, default=0) + 1


def calculate_next_slot(posts_data: dict) -> str:
    """Find next available posting time."""
    niche = get_niche(NICHE_ID)
    posting_times = niche.get("engagement", {}).get("posting_times", ["11:00", "18:00"])

    # Get all scheduled times
    scheduled = set()
    for p in posts_data.get("posts", []):
        sf = p.get("scheduled_for")
        if sf and p.get("status") in ("approved", "draft"):
            scheduled.add(sf[:16])  # YYYY-MM-DDTHH:MM

    now = datetime.now(ET)
    candidate = now

    for _ in range(100):  # max 50 days ahead
        for time_str in posting_times:
            h, m = time_str.split(":")
            base_slot = candidate.replace(hour=int(h), minute=int(m), second=0, microsecond=0)

            # Apply jitter before checking collisions
            jitter = random.randint(-30, 30)
            slot = base_slot + timedelta(minutes=jitter)

            if slot <= now:
                continue

            slot_key = slot.isoformat()[:16]
            if slot_key not in scheduled:
                return slot.isoformat()

        candidate += timedelta(days=1)

    # Fallback
    return (now + timedelta(days=1)).isoformat()


def add_to_queue(posts_data: dict, story: dict, obj: MuseumObject) -> dict:
    """Convert a generated story into a posts.json entry."""
    post_id = next_post_id(posts_data)
    scheduled = calculate_next_slot(posts_data)
    tweets = story["tweets"]
    is_thread = len(tweets) > 1

    # Build the post entry
    post = {
        "id": post_id,
        "type": "museum",
        "object_id": obj.id,
        "museum": obj.museum,
        "title": obj.title,
        "artist": obj.artist,
        "date": obj.date,
        "medium": obj.medium,
        "culture": obj.culture,
        "period": obj.period,
        "category": _classify_category(obj),
        "thread": is_thread,
        "tweets": [
            {
                "text": t["text"],
                "image_url": t.get("image_url"),
            }
            for t in tweets
        ],
        # Flat fields for compatibility with post.py
        "text": tweets[0]["text"],
        "image_urls": [t.get("image_url") for t in tweets if t.get("image_url")],
        "status": "draft",
        "scheduled_for": scheduled,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "object_url": obj.object_url,
        "score": None,
    }

    posts_data["posts"].append(post)
    return post


def _classify_category(obj: MuseumObject) -> str:
    """Classify object into broad category for diversity tracking."""
    classification = (obj.classification or "").lower()
    medium = (obj.medium or "").lower()
    dept = (obj.department or "").lower()
    tags = " ".join(obj.tags).lower()
    all_text = f"{classification} {medium} {dept} {tags}"

    if any(w in all_text for w in ["painting", "oil on canvas", "watercolor", "fresco"]):
        return "painting"
    if any(w in all_text for w in ["sculpture", "statue", "bust", "relief", "figure"]):
        return "sculpture"
    if any(w in all_text for w in ["weapon", "armor", "sword", "dagger", "shield", "arms"]):
        return "weapons"
    if any(w in all_text for w in ["jewelry", "ring", "necklace", "brooch", "crown", "gold"]):
        return "jewelry"
    if any(w in all_text for w in ["textile", "silk", "tapestry", "fabric", "costume"]):
        return "textile"
    if any(w in all_text for w in ["ceramic", "pottery", "porcelain", "vase", "bowl"]):
        return "ceramics"
    if any(w in all_text for w in ["photograph", "photo"]):
        return "photography"
    if any(w in all_text for w in ["print", "woodcut", "etching", "lithograph"]):
        return "prints"
    if any(w in all_text for w in ["furniture", "chair", "table", "cabinet", "desk"]):
        return "furniture"
    if any(w in all_text for w in ["mask", "ritual", "ceremony"]):
        return "ritual"
    return "other"


# --- Main pipeline ---

def main():
    parser = argparse.ArgumentParser(description="Fetch museum content and generate posts")
    parser.add_argument("--niche", default=NICHE_ID, help="Niche ID (default: museumstories)")
    parser.add_argument("--dry-run", action="store_true", help="Show candidates without generating stories")
    parser.add_argument("--batch-size", type=int, default=4, help="Number of posts to generate")
    parser.add_argument("--auto-approve", action="store_true", help="Set status to approved instead of draft")
    args = parser.parse_args()

    niche = get_niche(args.niche)
    museum_config = niche.get("museum_config", {})

    posts_data = load_posts()
    pending = [p for p in posts_data["posts"] if p.get("status") in ("draft", "approved")]

    min_queue = museum_config.get("min_queue_size", 6)
    if len(pending) >= min_queue and not args.dry_run:
        log.info(f"Queue has {len(pending)} pending posts (min {min_queue}). No fetch needed.")
        return

    log.info(f"Queue: {len(pending)} pending (min {min_queue}). Fetching new content...")

    # 1. Discover candidates
    candidates = fetch_candidates()
    if not candidates:
        log.warning("No candidates found from any API")
        notify("museum: no candidates", "All museum API searches returned 0 results")
        return

    # 2. Score and rank
    post_history = get_post_history(posts_data)
    ranked = filter_and_rank(candidates, post_history)
    if not ranked:
        log.warning("No candidates passed quality threshold")
        return

    if args.dry_run:
        print(f"\n{'='*60}")
        print(f"DRY RUN — Top {min(10, len(ranked))} candidates:")
        print(f"{'='*60}")
        for i, obj in enumerate(ranked[:10], 1):
            fmt = decide_format(obj)
            print(f"\n{i}. [{obj._total_score:.0f}] {obj.museum.upper()}: {obj.title}")
            print(f"   Artist: {obj.artist or 'Unknown'} | Date: {obj.date or '?'}")
            print(f"   Format: {fmt} | Images: {1 + len(obj.additional_images)}")
            if obj.fun_fact:
                print(f"   Fun fact: {obj.fun_fact[:100]}...")
            if obj.description:
                print(f"   Description: {obj.description[:100]}...")
        print(f"{'='*60}")
        return

    # 3. Generate stories
    batch_size = args.batch_size
    generated = 0
    failures = 0
    max_failures = 5

    for obj in ranked:
        if generated >= batch_size:
            break
        if failures >= max_failures:
            log.warning(f"Too many failures ({failures}), stopping batch")
            break

        fmt = decide_format(obj)
        log.info(f"Generating {fmt} for: {obj.title} ({obj.museum})")

        story = generate_story(obj, fmt)
        if not story:
            failures += 1
            continue

        post = add_to_queue(posts_data, story, obj)
        if args.auto_approve:
            post["status"] = "approved"

        generated += 1
        log.info(f"  Added post #{post['id']}: {post['title']} ({fmt}, scheduled {post['scheduled_for'][:16]})")

        # Show generated tweets
        for i, tweet in enumerate(story["tweets"], 1):
            log.info(f"    Tweet {i}: [{len(tweet['text'])}c] {tweet['text'][:80]}...")

    save_posts(posts_data)

    pending_after = len([p for p in posts_data["posts"] if p.get("status") in ("draft", "approved")])
    log.info(f"Done. Generated {generated} posts. Queue now: {pending_after} pending.")

    if generated > 0:
        notify("museum: content generated", f"{generated} new posts added to queue ({pending_after} total pending)")


if __name__ == "__main__":
    lock_fd = acquire_lock(BASE_DIR / ".museum_fetch.lock")
    if not lock_fd:
        log.info("Another museum_fetch is already running, exiting")
        sys.exit(0)
    try:
        main()
    finally:
        release_lock(lock_fd)
