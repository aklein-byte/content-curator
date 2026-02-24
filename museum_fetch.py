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
import re
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
from tools.common import load_json, save_json, notify, acquire_lock, release_lock, setup_logging, get_anthropic, load_voice_guide
from config.niches import get_niche

log = setup_logging("museum_fetch")

BASE_DIR = Path(__file__).parent
ET = ZoneInfo("America/New_York")

NICHE_ID = "museumstories"

# --- NIMA/TOPIQ aesthetic scoring (VPS only) ---

_nima_model = None
_topiq_model = None
_aesthetic_models_loaded = False
_aesthetic_models_available = False


def _load_aesthetic_models():
    """Lazy singleton loader for NIMA + TOPIQ models. VPS only."""
    global _nima_model, _topiq_model, _aesthetic_models_loaded, _aesthetic_models_available
    if _aesthetic_models_loaded:
        return _aesthetic_models_available
    _aesthetic_models_loaded = True
    try:
        import pyiqa
        import torch
        device = torch.device("cpu")
        _nima_model = pyiqa.create_metric("nima", device=device)
        _topiq_model = pyiqa.create_metric("topiq_iaa", device=device)
        _aesthetic_models_available = True
        log.info("NIMA + TOPIQ models loaded")
    except ImportError:
        log.info("pyiqa not available (local dev), skipping aesthetic scoring")
    except Exception as e:
        log.warning(f"Failed to load aesthetic models: {e}")
    return _aesthetic_models_available


def score_image_aesthetics(obj: MuseumObject) -> tuple[float | None, float | None]:
    """Score primary image with NIMA + TOPIQ. Returns (nima_score, topiq_score) or (None, None)."""
    if not _load_aesthetic_models():
        return None, None

    if not obj.primary_image_url:
        return None, None

    import tempfile
    import urllib.request
    try:
        import torch
        from PIL import Image

        # Download to temp file (with User-Agent for APIs that require it)
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
            tmp_path = tmp.name
            req = urllib.request.Request(
                obj.primary_image_url,
                headers={"User-Agent": "MuseumStories/1.0 (museum content bot)"},
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                tmp.write(resp.read())

        # Score
        nima_score = _nima_model(tmp_path).item()
        topiq_score = _topiq_model(tmp_path).item()

        # Cleanup
        os.unlink(tmp_path)

        return nima_score, topiq_score
    except Exception as e:
        log.warning(f"Aesthetic scoring failed for {obj.title}: {e}")
        try:
            os.unlink(tmp_path)
        except Exception:
            pass
        return None, None


# Use shared Anthropic singleton from tools.common
_get_anthropic_client = get_anthropic


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
    # Textiles & decorative arts (underrepresented)
    "tapestry", "embroidery", "kimono", "carpet", "velvet", "lace",
    # Ceramics & glass
    "porcelain", "stoneware", "faience", "glasswork", "enamel",
    # Furniture & instruments
    "harpsichord", "violin", "lute", "cabinet", "clock", "automaton",
    # Manuscripts & prints
    "manuscript", "illuminated", "woodblock", "calligraphy", "scroll",
    # Jewelry & metalwork
    "brooch", "tiara", "cameo", "inlay", "filigree",
    # Ritual & sacred
    "reliquary", "altar", "incense", "devotional", "votive",
    # Photography
    "daguerreotype", "photograph", "albumen",
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
    # New: underrepresented categories
    {"query": "Ottoman calligraphy", "period": "islamic"},
    {"query": "African textile kente", "period": "african"},
    {"query": "Japanese woodblock ukiyo-e", "period": "asian"},
    {"query": "pre-Columbian gold", "period": "precolumbian"},
    {"query": "Art Nouveau jewelry Lalique", "period": "modern"},
    {"query": "Chinese jade carving", "period": "asian"},
    {"query": "Persian manuscript illumination", "period": "islamic"},
    {"query": "Benin bronze", "period": "african"},
    {"query": "mechanical automaton clock", "period": "modern"},
    {"query": "Meissen porcelain", "period": "baroque"},
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

def score_story_potential(obj: MuseumObject) -> int | None:
    """Use Haiku to quickly score story potential (1-10). Returns score or None on failure.
    Cheap gate (~$0.001/call) to prevent wasted Opus calls on boring objects."""
    try:
        client = _get_anthropic_client()

        meta_parts = [f"Title: {obj.title}"]
        if obj.artist:
            meta_parts.append(f"Artist: {obj.artist}")
        if obj.date:
            meta_parts.append(f"Date: {obj.date}")
        if obj.medium:
            meta_parts.append(f"Medium: {obj.medium}")
        if obj.culture:
            meta_parts.append(f"Culture: {obj.culture}")
        if obj.description:
            meta_parts.append(f"Description: {obj.description[:500]}")
        if obj.fun_fact:
            meta_parts.append(f"Fun fact: {obj.fun_fact[:300]}")
        if obj.did_you_know:
            meta_parts.append(f"Did you know: {obj.did_you_know[:300]}")
        if obj.tags:
            meta_parts.append(f"Tags: {', '.join(obj.tags[:8])}")

        prompt = f"""Score this museum object 1-10 on story potential for a social media post.

{chr(10).join(meta_parts)}

Does this object have a surprising fact, dramatic history, unusual material, or famous connection?
A 7+ means there's a clear hook for a compelling post. A 6 or below means it's generic or academic.
Return just the number, nothing else."""

        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=10,
            messages=[{"role": "user", "content": prompt}],
        )
        text = response.content[0].text.strip()
        # Extract first number
        match = re.search(r'\d+', text)
        if match:
            return int(match.group())
        return None
    except Exception as e:
        log.warning(f"Story scoring failed for {obj.title}: {e}")
        return None


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
    """0-100 score based on available images and aesthetic scores.
    Uses NIMA/TOPIQ when available (VPS), falls back to image count heuristic."""
    if not obj.primary_image_url:
        return 0.0

    # Use aesthetic scores if already computed
    nima = getattr(obj, '_nima_score', None)
    topiq = getattr(obj, '_topiq_score', None)
    if nima is not None and topiq is not None:
        # Map NIMA (1-10 scale) and TOPIQ (1-10 scale) to 0-100
        # NIMA: 4.5 threshold → 45, 6.0 → 80, 7.0 → 100
        nima_norm = min(100, max(0, (nima - 3.0) * 25))
        topiq_norm = min(100, max(0, (topiq - 3.0) * 25))
        score = nima_norm * 0.5 + topiq_norm * 0.5
        # Bonus for extra images (multi-image posts perform much better)
        n_additional = len(obj.additional_images)
        if n_additional >= 1:
            score = min(100, score + 8)
        if n_additional >= 2:
            score = min(100, score + 7)
        if n_additional >= 3:
            score = min(100, score + 5)
        return score

    # Fallback: simple count-based heuristic (local dev)
    score = 50.0
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
    """Boost scores for underrepresented categories and museums."""
    recent = post_history[-30:] if post_history else []
    if not recent:
        return candidates

    museum_counts = Counter(p.get("museum") for p in recent if p.get("museum"))
    category_counts = Counter(p.get("category") for p in recent if p.get("category"))
    total = len(recent) or 1

    for obj in candidates:
        boost = 1.0

        # Museum diversity: penalize overrepresented, boost underrepresented
        museum_freq = museum_counts.get(obj.museum, 0) / total
        if museum_freq < 0.1:
            boost *= 1.4  # Never seen this museum, big boost
        elif museum_freq < 0.2:
            boost *= 1.2
        elif museum_freq > 0.4:
            boost *= 0.6  # Too many from this museum
        elif museum_freq > 0.3:
            boost *= 0.8

        # Category diversity: strong penalty for overrepresented, boost rare types
        cat = _classify_category(obj)
        cat_freq = category_counts.get(cat, 0) / total
        if cat_freq > 0.25:
            boost *= 0.4  # Strong penalty: >25% of last 30 posts
        elif cat == "painting" and cat_freq > 0.2:
            boost *= 0.6  # Extra painting penalty
        elif cat_freq < 0.05:
            boost *= 1.4  # Never or rarely seen, big boost
        elif cat_freq < 0.1:
            boost *= 1.3  # Rare category, boost it

        # Boost non-paintings generically (they're underrepresented in APIs)
        if obj.medium and "oil on canvas" not in (obj.medium or "").lower():
            boost *= 1.1

        obj._diversity_boost = boost

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
    """Decide single tweet vs thread based on available material.
    Thread when: enough images AND enough story to fill 2-3 tweets without padding."""
    image_count = 1 + len(obj.additional_images)

    has_rich_story = (
        (obj.description and len(obj.description) > 300) or
        (obj.fun_fact and obj.did_you_know) or
        (obj.wall_description and len(obj.wall_description) > 200)
    )

    # Thread if we have 2+ distinct images and a rich story
    if image_count >= 2 and has_rich_story:
        return "thread"

    # Also thread if story material is very rich, even with 1 image
    # (the prompt will still assign different crops/angles if available)
    very_rich = (
        (obj.description and len(obj.description) > 500) and
        (obj.fun_fact or obj.did_you_know or (obj.wall_description and len(obj.wall_description) > 100))
    )
    if very_rich:
        return "thread"

    return "single"


# --- Story generation ---

def _load_voice_guide() -> str:
    return load_voice_guide(NICHE_ID)


def generate_story(obj: MuseumObject, fmt: str) -> dict | None:
    """Call Claude to generate post copy for a museum object.

    Returns:
        {
            "tweets": [{"text": "...", "image_urls": ["..."]}],
            "metadata": {"object_id": "...", "museum": "...", ...}
        }
    """
    client = _get_anthropic_client()
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

## IMAGE AWARENESS
You have {len(images)} image(s). {"The single image is a full-object shot. Do NOT describe fine details the viewer cannot see (engravings, inscriptions, brush strokes, small carvings). Describe what IS visible at normal zoom: overall shape, scale, color, material, condition. Save the detail for things the viewer can verify." if len(images) == 1 else "Multiple images available. If a detail image shows a close-up, you can describe fine details. Otherwise stick to what's visible."}

## INSTRUCTIONS

1. HOOK FIRST. Don't start with "This is [title]." Start with what makes this object interesting.
2. Include context the viewer can't see: backstory, technique, scandal, how it got to the museum.
3. Specific details: dates, dimensions, materials, names, costs.
4. Explain anything a layperson wouldn't know (but don't lecture).
5. If you reference another work, it must be in an image. Don't mention things you can't show.
6. Match your description to what the image actually shows. Don't oversell details the viewer can't verify.

## ENDING (before the signature line)

The last sentence before the attribution line is the most important. It's the line someone repeats at a dinner party. Use ONE of these patterns:

- A specific number that recontextualizes the story ("It sold for $3.2 million. The artist was paid $50.")
- A flat, deadpan status update ("It's now a parking garage." / "Nobody has tried since.")
- A zoom-out that reveals surprising scale ("This was one of 4,000 tiles. Each one different.")
- A genuine unanswered mystery, not a rhetorical question ("Why he stopped painting, nobody knows.")
- A connection to the present day in one concrete sentence ("The family still owns the building.")
- A brief gut reaction, 1-5 words max, that only works after the full story ("Gone. All of it.")

NEVER end with: abstract lessons, empty superlatives ("remarkable", "incredible"), "And that's the story of...", rhetorical questions, philosophy, platitudes ("some things never change"), or telling the reader what to feel.

## CRITICAL: AVOID AI WRITING PATTERNS

Your writing WILL be rejected if it contains any of these. Read this section carefully.

STRUCTURAL PATTERNS THAT ARE INSTANT REJECTS:
- "The real X isn't Y, it's Z" or any variant ("The real journey isn't...", "The real story isn't...")
- "Not X. It's Y." or "Isn't X. It's Y." — negative parallelism for fake profundity
- "More than just a [noun]" or "Not just a [noun]"
- Present-participle tack-ons: "...creating a sense of...", "...making it one of...", "...transforming the..."
- Starting sentences with "This" referring to the previous idea ("This wasn't just..." "This single act...")
- Rhetorical questions meant to sound profound ("But what makes this truly remarkable?")
- "Whether you're X or Y, this Z..."
- Significance claims: "highlighting the importance of", "underscoring", "a reminder that"
- Philosophical wrap-ups: "perhaps the real...", "in a way, it...", "it reminds us that..."
- "What makes this X special/remarkable/unique is..."
- Excessive compound sentences that try to do two things at once

BAD (AI-sounding):
- "The real journey isn't the one shown. It's the cultural path these stories traveled across continents."
- "This wasn't just a painting. It was an act of artistic rebellion, one that would forever transform the Parisian art world."
- "More than just a decorative object, this 14th-century flask represents the pinnacle of Islamic metalwork, combining artistry with function."
- "What makes this piece truly remarkable is not its beauty, but the story of how it survived."

GOOD (human-sounding):
- "William is 4,000 years old, 8 inches long, and the most famous hippo in New York."
- "A painter with no commission destroyed France's most famous living artist. The weapon: this painting."
- "This flask weighs 3 pounds. Every inch is inlaid with silver. It took a metalworker in Damascus roughly 6 months."
- "The British Museum bought it in 1897 for 12 pounds. It's now worth more than most houses in London."

Notice the difference: good writing SHOWS with facts. Bad writing TELLS you what to think.

## OUTPUT
Return valid JSON only:

{{
  "tweets": [
    {{"text": "Tweet text here", "image_url": "https://..." or null}}
  ]
}}

CRITICAL: The image_url goes in the JSON field ONLY. Do NOT paste image URLs into the tweet text. The text should contain zero URLs.
No markdown, no explanation, just the JSON."""

    system = f"""You write posts for @museumstories on X/Twitter. Compelling narrator voice.

{niche['writer_prompt']}

{voice_guide}"""

    try:
        response = client.messages.create(
            model="claude-opus-4-6",
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

        # Clean: strip any URLs that Claude embedded in tweet text
        for tweet in story["tweets"]:
            cleaned = re.sub(r'\s*https?://\S+', '', tweet["text"]).strip()
            if cleaned != tweet["text"]:
                log.info(f"Stripped URL from tweet text for {obj.title}")
                tweet["text"] = cleaned

        # Validate: reject banned words
        banned = ["delve", "tapestry", "vibrant", "realm", "nestled", "testament", "beacon",
                  "multifaceted", "landscape", "groundbreaking", "fostering", "leveraging",
                  "spearheading", "navigating", "game-changer", "revolutionize", "cutting-edge"]
        banned_phrases = ["not just", "more than just", "isn't merely", "a testament to",
                         "a beacon of", "at the heart of", "rich tapestry", "diverse range",
                         "in today's", "it's important to note", "it's worth noting",
                         "stands as a", "whether you're", "valuable insights",
                         "resonate with", "align with"]
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

        # Validate: reject AI structural patterns (regex)
        ai_patterns = [
            (r"the real \w+ (?:isn't|wasn't|isn't|wasn't)", "the real X isn't Y"),
            (r"(?:not|isn't|wasn't|isn't|wasn't) [\w\s,]+\. it'?s ", "negative parallelism (not X. it's Y)"),
            (r"more than (?:just )?(?:a|an) \w+", "more than just a"),
            (r"what makes (?:this|it) [\w\s]+ (?:remarkable|special|unique|extraordinary)", "what makes this remarkable"),
            (r"(?:creating|making|transforming|establishing|forging|cementing|solidifying) (?:a|an|the|it) [\w\s]*(?:sense|space|legacy|symbol|reminder|testament)", "participle tack-on"),
            (r"perhaps (?:the|what|that)", "philosophical wrap-up"),
            (r"(?:it |this )reminds us", "philosophical wrap-up"),
            (r"in (?:a|some) (?:way|sense),", "hedging significance"),
            (r"(?:highlighting|underscoring|illustrating|demonstrating|showcasing) the (?:importance|significance|power|beauty)", "significance claim"),
            (r"(?:truly|genuinely) (?:remarkable|extraordinary|unique|special)", "vague superlative"),
            (r"but what (?:makes|made) (?:this|it)", "rhetorical question for profundity"),
        ]
        for pattern, label in ai_patterns:
            if re.search(pattern, all_lower):
                log.warning(f"AI pattern '{label}' in generated post for {obj.title} — rejecting")
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

        # QA check: Sonnet validates the draft is a coherent post
        image_urls_for_qa = [t.get("image_url") for t in story["tweets"] if t.get("image_url")]
        qa_ok, qa_reason = _validate_museum_draft(story, obj, image_urls_for_qa)
        if not qa_ok:
            log.warning(f"Draft QA failed for {obj.title}: {qa_reason}")
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


def _validate_museum_draft(story: dict, obj, image_urls: list[str]) -> tuple[bool, str]:
    """Use Sonnet to QA a museum draft before it enters the queue.

    Checks that the post:
    - Has real body text (not just a signature/attribution line)
    - Makes sense as a standalone post
    - Appropriately references what the images would show
    - Doesn't hallucinate wild claims
    """
    client = _get_anthropic_client()
    all_text = "\n---\n".join(t["text"] for t in story["tweets"])
    n_tweets = len(story["tweets"])
    format_label = f"{n_tweets}-tweet thread" if n_tweets > 1 else "single tweet"
    image_list = "\n".join(f"  {i+1}. {url}" for i, url in enumerate(image_urls))

    try:
        response = client.messages.create(
            model="claude-sonnet-4-6-20250514",
            max_tokens=256,
            messages=[{"role": "user", "content": f"""You are a QA reviewer for a museum/art social media account.

Object: {obj.title} by {obj.artist or 'Unknown'}
Date: {obj.date or 'Unknown'} | Medium: {obj.medium or 'Unknown'}
Museum: {obj.museum}

Images ({len(image_urls)}):
{image_list}

Draft post ({format_label}):
{all_text}

Answer these questions:
1. Does each tweet have real body text, or is any tweet just attribution/signature with no content?
2. Does the post make sense as something people would want to read?
3. If the post describes visual details, are they plausible for a museum object ({obj.medium or 'unknown medium'})?
4. Is the signature/attribution line (e.g. "Artist, Title, Year. Museum.") present and at the end, not the only content?

Respond with EXACTLY one line:
PASS — if the draft is good to post
FAIL: <brief reason> — if the draft should be rejected"""}],
        )
        result = response.content[0].text.strip().split("\n")[0]
        if result.startswith("PASS"):
            return True, "ok"
        else:
            reason = result.replace("FAIL:", "").replace("FAIL", "").strip() or "QA rejected"
            return False, reason
    except Exception as e:
        log.warning(f"Museum draft QA call failed: {e} — allowing draft through")
        return True, "qa-error-passthrough"


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
            "category": p.get("category"),
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
        "tweets": [],  # built below with images indices
        # Flat fields for compatibility with post.py
        "text": tweets[0]["text"],
        "image_urls": [t.get("image_url") for t in tweets if t.get("image_url")],
        "status": "draft",
        "scheduled_for": scheduled,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "object_url": obj.object_url,
        "score": None,
    }

    # Build tweets with images indices pointing into image_urls
    all_urls = post["image_urls"]
    for t in tweets:
        tw = {"text": t["text"], "image_url": t.get("image_url"), "images": []}
        if t.get("image_url") and t["image_url"] in all_urls:
            tw["images"] = [all_urls.index(t["image_url"])]
        post["tweets"].append(tw)

    posts_data["posts"].append(post)
    return post


def _classify_category(obj: MuseumObject) -> str:
    """Classify object into broad category for diversity tracking."""
    from config.categories import classify_museum_object
    classification = (obj.classification or "")
    medium = (obj.medium or "")
    dept = (obj.department or "")
    tags = " ".join(obj.tags)
    all_text = f"{classification} {medium} {dept} {tags}"
    return classify_museum_object(all_text)


# --- Main pipeline ---

def main():
    parser = argparse.ArgumentParser(description="Fetch museum content and generate posts")
    parser.add_argument("--niche", default=NICHE_ID, help="Niche ID (default: museumstories)")
    parser.add_argument("--dry-run", action="store_true", help="Show candidates without generating stories")
    parser.add_argument("--batch-size", type=int, default=4, help="Number of posts to generate")
    parser.add_argument("--auto-approve", action="store_true", help="Set status to approved instead of draft")
    parser.add_argument("--force", action="store_true", help="Bypass queue size check")
    args = parser.parse_args()

    niche = get_niche(args.niche)
    museum_config = niche.get("museum_config", {})

    posts_data = load_posts()
    pending = [p for p in posts_data["posts"] if p.get("status") in ("draft", "approved")]

    min_queue = museum_config.get("min_queue_size", 6)
    if len(pending) >= min_queue and not args.dry_run and not args.force:
        log.info(f"Queue has {len(pending)} pending posts (min {min_queue}). No fetch needed.")
        return

    log.info(f"Queue: {len(pending)} pending (min {min_queue}). Fetching new content...")

    # 1. Discover candidates
    candidates = fetch_candidates()
    if not candidates:
        log.warning("No candidates found from any API")
        notify("museum: no candidates", "All museum API searches returned 0 results")
        return

    # 1b. Aesthetic image gate (NIMA + TOPIQ, VPS only)
    if _load_aesthetic_models():
        passed = []
        for obj in candidates:
            nima, topiq = score_image_aesthetics(obj)
            if nima is not None and topiq is not None:
                obj._nima_score = nima
                obj._topiq_score = topiq
                if nima < 4.5 or topiq < 4.8:
                    log.info(f"  Rejected (aesthetic): {obj.title[:50]} — NIMA={nima:.2f} TOPIQ={topiq:.2f}")
                    continue
                log.info(f"  Passed (aesthetic): {obj.title[:50]} — NIMA={nima:.2f} TOPIQ={topiq:.2f}")
            passed.append(obj)
        log.info(f"Aesthetic gate: {len(passed)}/{len(candidates)} passed")
        candidates = passed
        if not candidates:
            log.warning("No candidates passed aesthetic gate")
            return

    # 1c. Story potential gate (Haiku, cheap pre-filter before Opus)
    story_passed = []
    for obj in candidates:
        story_score = score_story_potential(obj)
        if story_score is None:
            log.info(f"  Skipped (story scoring failed): {obj.title[:50]}")
            continue
        obj._story_score = story_score
        if story_score < 7:
            log.info(f"  Rejected (story): {obj.title[:50]} — score {story_score}/10")
            continue
        log.info(f"  Passed (story): {obj.title[:50]} — score {story_score}/10")
        story_passed.append(obj)
    log.info(f"Story gate: {len(story_passed)}/{len(candidates)} passed")
    candidates = story_passed
    if not candidates:
        log.warning("No candidates passed story potential gate")
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
            nima = getattr(obj, '_nima_score', None)
            topiq = getattr(obj, '_topiq_score', None)
            if nima is not None:
                print(f"   NIMA: {nima:.2f} | TOPIQ: {topiq:.2f}")
            story_score = getattr(obj, '_story_score', None)
            if story_score is not None:
                print(f"   Story potential: {story_score}/10")
            if obj.fun_fact:
                print(f"   Fun fact: {obj.fun_fact[:100]}...")
            if obj.description:
                print(f"   Description: {obj.description[:100]}...")
        print(f"{'='*60}")
        return

    # 2b. Format mix: ensure threads aren't underrepresented
    thread_count = sum(1 for p in pending if p.get("thread"))
    thread_pct = thread_count / max(len(pending), 1)
    force_thread_id = None
    if thread_pct < 0.15:
        # Find first ranked candidate with 2+ images to force as thread
        for obj in ranked:
            if 1 + len(obj.additional_images) >= 2:
                force_thread_id = obj.id
                log.info(f"Format mix: threads at {thread_pct:.0%}, forcing thread for {obj.title[:40]}")
                break

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

        fmt = "thread" if obj.id == force_thread_id else decide_format(obj)
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
