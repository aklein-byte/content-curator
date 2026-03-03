"""
NASA content pipeline for @CosmicShots_.

Fetches images from NASA Image API, scores for visual impact,
generates post copy with Claude, fact-checks, verifies image-tweet
match against NASA metadata, and adds to the posts queue.

Usage:
    python nasa_fetch.py [--dry-run] [--batch-size 4] [--niche cosmicshots]
"""

import sys
import os
import json
import re
import random
import logging
import argparse
import time
import urllib.parse
from pathlib import Path
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from dataclasses import dataclass, field
from io import BytesIO

sys.path.insert(0, str(Path(__file__).parent))

from dotenv import load_dotenv
load_dotenv()

from tools.common import (
    load_json, save_json, notify,
    setup_logging, get_anthropic, load_voice_guide, get_model,
)
from tools.post_queue import (
    resolve_posts_file as pq_resolve_posts_file,
    load_posts as pq_load_posts, save_posts as pq_save_posts,
    next_post_id, next_schedule_slot,
)
from config.niches import get_niche
from agents.fact_checker import fact_check_draft, quick_validate, SourceContext

log = setup_logging("nasa_fetch")

BASE_DIR = Path(__file__).parent
ET = ZoneInfo("America/New_York")

NICHE_ID = "cosmicshots"

# NASA Image API base
NASA_API = "https://images-api.nasa.gov"

# Minimum image dimensions (pixels) — reject anything smaller
MIN_IMAGE_DIMENSION = 800
MIN_IMAGE_BYTES = 100_000  # 100KB — reject tiny thumbnails

# URL pattern for NASA image assets
NASA_ASSET_BASE = "https://images-assets.nasa.gov/image"


# --- NASA Image API ---

@dataclass
class NASAImage:
    """A candidate image from NASA Image API."""
    nasa_id: str
    title: str
    description: str = ""
    center: str = ""
    keywords: list[str] = field(default_factory=list)
    date_created: str = ""
    media_type: str = "image"
    photographer: str = ""
    image_url: str = ""
    thumb_url: str = ""

    # Scoring attributes (set during pipeline)
    _story_score: int = 0
    _image_size: int = 0
    _image_width: int = 0
    _image_height: int = 0
    _total_score: float = 0.0


def nasa_search(query: str, limit: int = 20) -> list[NASAImage]:
    """Search NASA Image API. Returns up to `limit` NASAImage objects."""
    import requests

    encoded = urllib.parse.quote(query)
    url = f"{NASA_API}/search?q={encoded}&media_type=image&page_size={min(limit, 100)}"

    try:
        r = requests.get(url, timeout=20)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        log.warning(f"NASA search failed for '{query}': {e}")
        return []

    results = []
    for item in data.get("collection", {}).get("items", []):
        if not item.get("data"):
            continue
        d = item["data"][0]

        if d.get("media_type") != "image":
            continue

        nasa_id = d.get("nasa_id", "")
        if not nasa_id:
            continue

        thumb = ""
        for link in item.get("links", []):
            if link.get("rel") == "preview":
                thumb = link.get("href", "")
                break

        results.append(NASAImage(
            nasa_id=nasa_id,
            title=d.get("title", ""),
            description=d.get("description", "")[:2000],
            center=d.get("center", ""),
            keywords=d.get("keywords", [])[:15],
            date_created=d.get("date_created", ""),
            photographer=d.get("photographer", ""),
            thumb_url=thumb,
        ))

    return results[:limit]


def resolve_image_url(nasa_id: str) -> str | None:
    """Find the best high-res image URL for a NASA ID.

    Tries ~orig.jpg first, then ~large.jpg. Returns None if neither works.
    """
    import requests

    for suffix in ["~orig.jpg", "~large.jpg", "~orig.png", "~large.png"]:
        url = f"{NASA_ASSET_BASE}/{nasa_id}/{nasa_id}{suffix}"
        try:
            r = requests.head(url, timeout=10, allow_redirects=True)
            if r.status_code == 200:
                size = int(r.headers.get("content-length", 0))
                if size >= MIN_IMAGE_BYTES:
                    return url
                else:
                    log.debug(f"  {nasa_id}{suffix}: too small ({size} bytes)")
        except Exception:
            continue

    # Fallback: query asset manifest
    try:
        r = requests.get(f"{NASA_API}/asset/{nasa_id}", timeout=10)
        if r.status_code == 200:
            items = r.json().get("collection", {}).get("items", [])
            best_url = None
            best_size = 0
            for item in items:
                href = item.get("href", "")
                if any(href.lower().endswith(ext) for ext in [".jpg", ".jpeg", ".png"]):
                    if "thumb" in href.lower() or "small" in href.lower():
                        continue
                    try:
                        hr = requests.head(href, timeout=10)
                        sz = int(hr.headers.get("content-length", 0))
                        if sz > best_size:
                            best_size = sz
                            best_url = href
                    except Exception:
                        continue
            if best_url and best_size >= MIN_IMAGE_BYTES:
                return best_url
    except Exception as e:
        log.debug(f"Asset manifest failed for {nasa_id}: {e}")

    return None


def check_image_dimensions(url: str) -> tuple[int, int] | None:
    """Download image headers and check pixel dimensions. Returns (width, height) or None."""
    import requests
    try:
        from PIL import Image

        r = requests.get(url, timeout=30, stream=True)
        data = b""
        for chunk in r.iter_content(chunk_size=8192):
            data += chunk
            if len(data) > 65536:
                break
        r.close()

        img = Image.open(BytesIO(data))
        return img.size
    except Exception as e:
        log.debug(f"Dimension check failed for {url}: {e}")
        return None


def verify_image_matches_tweet(nasa_id: str, tweet_text: str) -> tuple[bool, str]:
    """Cross-check NASA API metadata against tweet content.

    Queries NASA API for the nasa_id, extracts title/description/keywords,
    then uses Claude Haiku to verify the tweet describes the same subject.

    Returns (passed, reason).
    """
    import requests

    try:
        url = f"{NASA_API}/search?nasa_id={nasa_id}"
        r = requests.get(url, timeout=15)
        items = r.json().get("collection", {}).get("items", [])
        if not items:
            return False, f"NASA ID {nasa_id} not found in API"

        d = items[0]["data"][0]
        api_title = d.get("title", "")
        api_desc = d.get("description", "")[:500]
        api_keywords = d.get("keywords", [])
    except Exception as e:
        return False, f"NASA API query failed: {e}"

    try:
        client = get_anthropic()
        response = client.messages.create(
            model=get_model("scorer"),
            max_tokens=50,
            messages=[{"role": "user", "content": f"""Does this tweet describe the same subject as this NASA image?

NASA IMAGE METADATA:
- Title: {api_title}
- Description: {api_desc[:300]}
- Keywords: {', '.join(api_keywords[:10])}

TWEET TEXT:
{tweet_text}

Answer YES or NO, then a brief reason. Format: YES/NO: reason"""}],
        )
        answer = response.content[0].text.strip()
        passed = answer.upper().startswith("YES")
        return passed, answer
    except Exception as e:
        log.warning(f"Image verification LLM call failed: {e}")
        return True, f"LLM verification skipped: {e}"


# --- Discovery ---

SEARCH_QUERIES = [
    "JWST deep field galaxies", "james webb nebula infrared",
    "hubble nebula", "hubble galaxy cluster", "hubble deep field",
    "hubble supernova remnant", "hubble planetary nebula",
    "juno jupiter close", "cassini saturn rings",
    "cassini enceladus", "new horizons pluto surface",
    "messenger mercury color", "perseverance mars landscape",
    "curiosity mars panorama", "SDO sun ultraviolet",
    "ISS earth night city lights", "ISS aurora",
    "rosetta comet 67P", "crab nebula composite",
    "andromeda galaxy infrared", "orion nebula hubble",
]


def fetch_candidates(search_queries: list[str] | None = None,
                     max_per_query: int = 15) -> list[NASAImage]:
    """Search NASA API across multiple queries. Returns deduplicated candidates."""
    queries = search_queries or SEARCH_QUERIES
    selected = random.sample(queries, k=min(4, len(queries)))

    candidates = []
    seen_ids = set()

    for query in selected:
        results = nasa_search(query, limit=max_per_query)
        for img in results:
            if img.nasa_id not in seen_ids:
                seen_ids.add(img.nasa_id)
                candidates.append(img)
        log.info(f"Query '{query}': {len(results)} results")
        time.sleep(0.5)

    log.info(f"Total unique candidates: {len(candidates)}")
    return candidates


# --- Quality gates ---

def is_artist_rendering(img: NASAImage) -> bool:
    """Detect artist illustrations/CGI. We only want real instrument data."""
    signals = [
        "artist" in img.title.lower(),
        "illustration" in img.title.lower(),
        "concept" in img.title.lower() and "artist" in img.description.lower(),
        "artist's" in img.description.lower()[:200],
        "illustration" in img.description.lower()[:200],
    ]
    kw_lower = [k.lower() for k in img.keywords]
    if "artist concept" in kw_lower or "illustration" in kw_lower:
        signals.append(True)
    return any(signals)


def score_story_potential(img: NASAImage) -> int | None:
    """Use Haiku to score visual + story potential (1-10)."""
    try:
        client = get_anthropic()
        niche = get_niche(NICHE_ID)

        meta_parts = [f"Title: {img.title}"]
        if img.description:
            meta_parts.append(f"Description: {img.description[:500]}")
        if img.keywords:
            meta_parts.append(f"Keywords: {', '.join(img.keywords[:10])}")
        if img.center:
            meta_parts.append(f"NASA Center: {img.center}")
        if img.date_created:
            meta_parts.append(f"Date: {img.date_created[:10]}")

        prompt = f"""Score this NASA image 1-10 for a space photography account on X/Twitter.

{chr(10).join(meta_parts)}

{niche['curator_prompt']}

Return just the number, nothing else."""

        response = client.messages.create(
            model=get_model("scorer"),
            max_tokens=10,
            messages=[{"role": "user", "content": prompt}],
        )
        text = response.content[0].text.strip()
        match = re.search(r'\d+', text)
        if match:
            return int(match.group())
        return None
    except Exception as e:
        log.warning(f"Story scoring failed for {img.title}: {e}")
        return None


def validate_image_quality(img: NASAImage) -> bool:
    """Check image resolution and size. Rejects low-res images.

    Sets img.image_url, img._image_width, img._image_height.
    Returns True if passes.
    """
    url = resolve_image_url(img.nasa_id)
    if not url:
        log.info(f"  Rejected (no valid image): {img.nasa_id} -- {img.title[:50]}")
        return False

    img.image_url = url

    dims = check_image_dimensions(url)
    if dims:
        w, h = dims
        img._image_width = w
        img._image_height = h
        if w < MIN_IMAGE_DIMENSION and h < MIN_IMAGE_DIMENSION:
            log.info(f"  Rejected (low-res {w}x{h}): {img.nasa_id} -- {img.title[:50]}")
            return False
    return True


def score_novelty(img: NASAImage, post_history: list[dict]) -> float:
    """0-100 score. Penalize images similar to recent posts."""
    score = 100.0
    if not post_history:
        return score

    all_ids = {p.get("nasa_id") for p in post_history}
    if img.nasa_id in all_ids:
        return 0.0

    recent = post_history[-10:]
    title_lower = img.title.lower()
    for p in recent:
        ptitle = (p.get("title") or "").lower()
        missions = ["juno", "cassini", "hubble", "jwst", "webb", "perseverance",
                     "curiosity", "voyager", "new horizons", "messenger", "sdo",
                     "rosetta", "chandra", "spitzer"]
        for m in missions:
            if m in title_lower and m in ptitle:
                score -= 15
                break

    return max(score, 0.0)


def filter_and_rank(candidates: list[NASAImage],
                    post_history: list[dict]) -> list[NASAImage]:
    """Score, filter, and rank candidates. Returns top 10."""
    for img in candidates:
        novelty = score_novelty(img, post_history)
        story = getattr(img, '_story_score', 5) or 5
        img._total_score = (story * 10) * 0.6 + novelty * 0.4

    candidates.sort(key=lambda x: x._total_score, reverse=True)
    qualified = [c for c in candidates if c._total_score >= 40.0]

    if qualified:
        log.info("Top 5 candidates:")
        for img in qualified[:5]:
            log.info(f"  [{img._total_score:.0f}] {img.nasa_id}: {img.title[:60]} "
                     f"(story={img._story_score})")

    return qualified[:10]


# --- Story generation ---

def generate_story(img: NASAImage) -> dict | None:
    """Call Claude to generate post copy for a NASA image."""
    client = get_anthropic()
    niche = get_niche(NICHE_ID)
    voice_guide = load_voice_guide(NICHE_ID)

    meta_parts = [
        f"NASA Image ID: {img.nasa_id}",
        f"Title: {img.title}",
    ]
    if img.description:
        meta_parts.append(f"Description: {img.description[:1500]}")
    if img.keywords:
        meta_parts.append(f"Keywords: {', '.join(img.keywords[:10])}")
    if img.center:
        meta_parts.append(f"NASA Center: {img.center}")
    if img.date_created:
        meta_parts.append(f"Date Created: {img.date_created[:10]}")
    if img.photographer:
        meta_parts.append(f"Photographer/Credit: {img.photographer}")

    metadata_block = "\n".join(meta_parts)

    prompt = f"""## NASA IMAGE METADATA
{metadata_block}

## IMAGE URL
{img.image_url}

## FORMAT
Write a SINGLE TWEET. This is a Premium account, no 280 char limit.
Write as long as the story needs. 300-600 chars is typical. Let the story breathe.

## INSTRUCTIONS

1. HOOK FIRST. The object, the distance, the instrument. Don't start with "This image shows..."
2. One wild physical fact. How big, how far, how hot, how fast, how old. Use real numbers.
3. The engineering story: what it took to capture this. Mission name, instrument, distance, exposure.
4. Numbers. Always numbers. Miles, degrees, years, pixels, dollars.
5. Short sentences. Fragments OK.
6. Last line: credit the mission and instrument (e.g., "NASA/JPL-Caltech/SwRI/MSSS.").

## FACTUAL ACCURACY
- Only state facts from the metadata above or well-known public knowledge about the mission.
- If the metadata doesn't specify something, don't invent it.
- Get the mission/instrument name right.
- The image MUST match what you describe.

## CRITICAL: AVOID AI WRITING PATTERNS

INSTANT REJECTS:
- "The real X isn't Y, it's Z"
- "Not X. It's Y." negative parallelism
- "More than just a [noun]"
- Rhetorical questions
- Significance claims: "highlighting", "underscoring", "a reminder that"
- Philosophical wrap-ups: "perhaps the real...", "reminds us that..."
- Em-dashes (use periods or commas instead)

NEVER USE: "awe-inspiring", "breathtaking", "the cosmos", "the vastness of space",
"humbling", "we are small", "pale blue dot" (unless literally about that photo)

## OUTPUT
Return valid JSON only:

{{"tweets": [{{"text": "Tweet text here", "image_url": "{img.image_url}"}}]}}

CRITICAL: image_url in JSON field ONLY. No URLs in tweet text.
No markdown, no explanation, just the JSON."""

    system = f"""You write posts for @CosmicShots_ about real space photography.

{niche['writer_prompt']}

{voice_guide}"""

    try:
        response = client.messages.create(
            model=get_model("writer"),
            max_tokens=1500,
            system=system,
            messages=[{"role": "user", "content": prompt}],
        )

        text = response.content[0].text.strip()

        json_start = text.find("{")
        if json_start < 0:
            log.error(f"No JSON in response for {img.title}")
            return None

        try:
            story = json.JSONDecoder().raw_decode(text, json_start)[0]
        except json.JSONDecodeError as e:
            log.error(f"JSON parse failed for {img.title}: {e}")
            return None

        if "tweets" not in story or not story["tweets"]:
            log.error(f"No tweets in response for {img.title}")
            return None

        # Clean: strip URLs from tweet text
        for tweet in story["tweets"]:
            cleaned = re.sub(r'\s*https?://\S+', '', tweet["text"]).strip()
            if cleaned != tweet["text"]:
                log.info(f"Stripped URL from tweet text for {img.title}")
                tweet["text"] = cleaned
            if not tweet.get("image_url"):
                tweet["image_url"] = img.image_url

        # --- IMAGE-TWEET VERIFICATION ---
        tweet_text = story["tweets"][0]["text"]
        match_ok, match_reason = verify_image_matches_tweet(img.nasa_id, tweet_text)
        if not match_ok:
            log.warning(f"Image-tweet mismatch for {img.nasa_id}: {match_reason}")
            return None
        log.info(f"  Image-tweet verified: {match_reason[:60]}")

        # --- FACT-CHECKING ---
        fc_source = SourceContext(
            fields={
                "title": img.title,
                "description": img.description[:1000],
                "keywords": ", ".join(img.keywords[:10]),
                "center": img.center,
                "date_created": img.date_created[:10] if img.date_created else "",
                "photographer": img.photographer,
            },
            object_title=img.title,
            niche_id=NICHE_ID,
        )
        story, _fc_verifications = fact_check_draft(story, fc_source, system)
        if story is None:
            log.warning(f"Fact-check rejected post for {img.title}")
            return None

        # --- HUMANIZER ---
        from tools.humanizer import validate_tweets
        hv = validate_tweets(story["tweets"])
        if not hv.passed:
            log.info(f"Humanizer issues for {img.title}: {', '.join(hv.violations[:3])}. Rewriting...")
            violation_list = "\n".join(f"- {v}" for v in hv.violations)
            try:
                fix_response = client.messages.create(
                    model=get_model("writer"),
                    max_tokens=1500,
                    system="You fix AI-sounding writing. Return ONLY valid JSON with the same structure.",
                    messages=[{"role": "user", "content": f"""Fix AI writing violations. Keep everything else identical.

Violations:
{violation_list}

Current draft:
{json.dumps(story, indent=2)}

Rules:
- Replace banned words with natural alternatives
- Replace em-dashes with periods or commas
- Rewrite AI-pattern sentences
- Do NOT change facts, dates, names
- Return same JSON structure"""}],
                )
                fixed_text = fix_response.content[0].text.strip()
                json_start = fixed_text.find("{")
                if json_start >= 0:
                    fixed_story = json.JSONDecoder().raw_decode(fixed_text, json_start)[0]
                    if "tweets" in fixed_story and fixed_story["tweets"]:
                        hv2 = validate_tweets(fixed_story["tweets"])
                        if hv2.passed:
                            story = fixed_story
                            log.info(f"Humanizer fix succeeded for {img.title}")
                        elif len(hv2.violations) < len(hv.violations):
                            story = fixed_story
                            log.info(f"Humanizer partial fix: {len(hv.violations)} -> {len(hv2.violations)}")
            except Exception as e:
                log.warning(f"Humanizer rewrite failed for {img.title}: {e}")

        # QA check
        all_text = " ".join(t["text"] for t in story["tweets"])
        qa_ok, qa_reason = quick_validate(all_text, fc_source)
        if not qa_ok:
            log.warning(f"Draft QA failed for {img.title}: {qa_reason}")
            return None

        return story

    except Exception as e:
        log.error(f"Story generation failed for {img.title}: {e}")
        return None


# --- Queue management ---

def load_posts() -> dict:
    return pq_load_posts(NICHE_ID)


def save_posts(data: dict):
    pq_save_posts(data, NICHE_ID)


def get_post_history(posts_data: dict) -> list[dict]:
    return [
        {"nasa_id": p.get("nasa_id"), "title": p.get("title")}
        for p in posts_data.get("posts", [])
    ]


def add_to_queue(posts_data: dict, story: dict, img: NASAImage) -> dict:
    """Convert a generated story into a posts.json entry."""
    post_id = next_post_id(posts_data)
    scheduled = next_schedule_slot(posts_data, NICHE_ID)
    tweets = story["tweets"]

    credit = img.center or "NASA"
    if img.photographer:
        credit = img.photographer

    post = {
        "id": post_id,
        "type": "original",
        "nasa_id": img.nasa_id,
        "title": img.title,
        "text": tweets[0]["text"],
        "tweets": [
            {
                "text": t["text"],
                "image_url": t.get("image_url", img.image_url),
                "images": [t.get("image_url", img.image_url)],
            }
            for t in tweets
        ],
        "image_urls": [t.get("image_url", img.image_url) for t in tweets],
        "status": "draft",
        "source": credit,
        "scheduled_for": scheduled,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "score": None,
    }

    posts_data["posts"].append(post)
    return post


# --- Main pipeline ---

def main():
    global NICHE_ID

    parser = argparse.ArgumentParser(description="Fetch NASA content and generate posts")
    parser.add_argument("--niche", default="cosmicshots", help="Niche ID")
    parser.add_argument("--dry-run", action="store_true", help="Show candidates without generating")
    parser.add_argument("--batch-size", type=int, default=4, help="Number of posts to generate")
    parser.add_argument("--force", action="store_true", help="Bypass queue size check")
    args = parser.parse_args()

    NICHE_ID = args.niche
    niche = get_niche(NICHE_ID)
    nasa_config = niche.get("nasa_config", {})

    posts_data = load_posts()
    pending = [p for p in posts_data["posts"] if p.get("status") in ("draft", "approved")]

    min_queue = nasa_config.get("min_queue_size", 10)
    if len(pending) >= min_queue and not args.dry_run and not args.force:
        log.info(f"Queue has {len(pending)} pending posts (min {min_queue}). No fetch needed.")
        return

    log.info(f"Queue: {len(pending)} pending (min {min_queue}). Fetching new content...")

    # 1. Discover candidates
    queries = nasa_config.get("search_queries", SEARCH_QUERIES)
    candidates = fetch_candidates(search_queries=queries)
    if not candidates:
        log.warning("No candidates found from NASA API")
        notify("nasa: no candidates", "All NASA API searches returned 0 results")
        return

    # 2. Filter artist renderings
    real_photos = [img for img in candidates if not is_artist_rendering(img)]
    log.info(f"Artist rendering filter: {len(real_photos)}/{len(candidates)} passed")
    candidates = real_photos
    if not candidates:
        log.warning("All candidates were artist renderings")
        return

    # 3. Image quality gate
    quality_passed = []
    for img in candidates:
        if validate_image_quality(img):
            quality_passed.append(img)
        time.sleep(0.3)
    log.info(f"Image quality gate: {len(quality_passed)}/{len(candidates)} passed")
    candidates = quality_passed
    if not candidates:
        log.warning("No candidates passed image quality gate")
        return

    # 4. Story potential gate (Haiku)
    story_passed = []
    for img in candidates:
        story_score = score_story_potential(img)
        if story_score is None:
            continue
        img._story_score = story_score
        if story_score < 7:
            log.info(f"  Rejected (story {story_score}/10): {img.title[:50]}")
            continue
        log.info(f"  Passed (story {story_score}/10): {img.title[:50]}")
        story_passed.append(img)
    log.info(f"Story gate: {len(story_passed)}/{len(candidates)} passed")
    candidates = story_passed
    if not candidates:
        log.warning("No candidates passed story potential gate")
        return

    # 5. Score and rank
    post_history = get_post_history(posts_data)
    ranked = filter_and_rank(candidates, post_history)
    if not ranked:
        log.warning("No candidates passed quality threshold")
        return

    if args.dry_run:
        print(f"\n{'='*60}")
        print(f"DRY RUN -- Top {min(10, len(ranked))} candidates:")
        print(f"{'='*60}")
        for i, img in enumerate(ranked[:10], 1):
            print(f"\n{i}. [{img._total_score:.0f}] {img.nasa_id}: {img.title}")
            print(f"   Keywords: {', '.join(img.keywords[:5])}")
            print(f"   Image: {img._image_width}x{img._image_height}")
            print(f"   Story: {img._story_score}/10")
            print(f"   URL: {img.image_url}")
        print(f"{'='*60}")
        return

    # 6. Generate stories
    batch_size = args.batch_size
    generated = 0
    failures = 0
    max_failures = 8

    for img in ranked:
        if generated >= batch_size:
            break
        if failures >= max_failures:
            log.warning(f"Too many failures ({failures}), stopping batch")
            break

        log.info(f"Generating post for: {img.title} ({img.nasa_id})")
        story = generate_story(img)
        if not story:
            failures += 1
            continue

        post = add_to_queue(posts_data, story, img)
        generated += 1
        log.info(f"  Added post #{post['id']}: {post['title']} (scheduled {post['scheduled_for'][:16]})")

        for tweet in story["tweets"]:
            log.info(f"    [{len(tweet['text'])}c] {tweet['text'][:80]}...")

    save_posts(posts_data)

    pending_after = len([p for p in posts_data["posts"] if p.get("status") in ("draft", "approved")])
    log.info(f"Done. Generated {generated} posts ({failures} failures). Queue: {pending_after} pending.")


if __name__ == "__main__":
    main()
