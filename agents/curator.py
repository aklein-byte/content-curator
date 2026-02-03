"""
Curator Agent - The critical agent that finds great images with taste.
Uses Claude Opus 4.5 for its ability to understand visual aesthetics,
cultural nuance, and what makes design photography compelling.
"""

import os
import json
import base64
import httpx
from anthropic import Anthropic
from dataclasses import dataclass
from typing import Optional

from config.sources import (
    ARCHITECTURE_FIRMS,
    ENGLISH_PUBLICATIONS,
    JAPANESE_SOURCES,
    HOSPITALITY_SOURCES,
    SEARCH_TERMS,
    QUALITY_CRITERIA,
    get_random_search_term,
)
from config.niches import get_niche
from tools.firecrawl import scrape_url, search_images
from tools.storage import add_candidate, is_image_known, log_source_scrape

# Use Opus 4.5 for curation - taste is critical
CURATOR_MODEL = "claude-opus-4-5-20251101"

client = Anthropic()


@dataclass
class CuratedImage:
    """An image found and evaluated by the curator."""
    image_url: str
    source_url: str
    source_name: str
    title: Optional[str]
    description: Optional[str]
    quality_score: int  # 1-10
    curator_notes: str
    scroll_stop_factor: str  # Why this would make someone pause


def build_curator_system_prompt(niche_id: str) -> str:
    """Build the system prompt for the curator agent."""
    niche = get_niche(niche_id)

    quality_positive = "\n".join(f"  - {q}" for q in QUALITY_CRITERIA["positive"])
    quality_negative = "\n".join(f"  - {q}" for q in QUALITY_CRITERIA["negative"])

    return f"""You are the curator agent for {niche['name']} ({niche['handle']}).

{niche['curator_prompt']}

## Quality Criteria

FIND images that have:
{quality_positive}

REJECT images that have:
{quality_negative}

## Your Task

When asked to find images, you will:
1. Search through provided sources or web search results
2. Evaluate each image against the quality criteria
3. Score each image 1-10 on "scroll-stop factor"
4. Only return images scoring 7 or higher
5. Provide detailed notes on why each image works

## Output Format

For each curated image, provide:
- image_url: Direct URL to the image
- source_url: URL of the page where you found it
- source_name: Name of the source (e.g., "Dezeen", "KKAA")
- title: Title or description from the source
- quality_score: 1-10 rating
- scroll_stop_factor: Why this image would make someone pause their feed
- curator_notes: Your assessment of the image

Return results as JSON array.

## Important

- Be extremely selective. It's better to return 2 great images than 10 mediocre ones.
- Reject anything that feels generic, staged, or AI-generated.
- Prefer authentic lived-in spaces over editorial perfection.
- Japanese-language sources that haven't been exposed to Western audiences are gold.
- If you can't find anything good, say so. Don't lower your standards.
"""


async def search_web_for_images(query: str, num_results: int = 5) -> list[dict]:
    """
    Use Anthropic's web search to find images.
    Returns list of search results with URLs.
    """
    # Use Firecrawl's search
    results = await search_images(query, num_results)
    return results


async def download_and_encode_image(url: str) -> Optional[tuple[str, str]]:
    """
    Download an image and return (base64_data, media_type).
    Returns None if download fails or image is too small/invalid.
    """
    async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as http:
        try:
            resp = await http.get(url, headers={
                "User-Agent": "Mozilla/5.0 (compatible; ContentCurator/1.0)"
            })
            if resp.status_code != 200:
                return None

            content_type = resp.headers.get("content-type", "")
            data = resp.content

            # Skip tiny images (< 10KB likely icons/thumbnails)
            if len(data) < 10_000:
                return None

            # Skip images > 5MB (API limit)
            if len(data) > 5_000_000:
                return None

            # Determine media type
            if "png" in content_type:
                media_type = "image/png"
            elif "webp" in content_type:
                media_type = "image/webp"
            elif "gif" in content_type:
                media_type = "image/gif"
            else:
                media_type = "image/jpeg"

            return base64.standard_b64encode(data).decode("utf-8"), media_type

        except Exception:
            return None


async def evaluate_images_from_source(
    source_url: str,
    source_name: str,
    niche_id: str,
) -> list[CuratedImage]:
    """
    Scrape a source, download images, and have Opus VISUALLY evaluate them.
    Uses Claude's vision API to actually see the images.
    """
    # Scrape the page
    page = await scrape_url(source_url)

    if not page.success:
        await log_source_scrape(source_url, source_name, 0, "failed")
        return []

    if not page.images:
        await log_source_scrape(source_url, source_name, 0, "success")
        return []

    # Filter out known images and small thumbnails
    new_images = []
    for img in page.images:
        url = img["url"]
        # Skip obvious thumbnails/icons by URL pattern
        if any(skip in url.lower() for skip in [
            "thumb", "icon", "logo", "avatar", "1x1", "pixel",
            "loading", "spinner", "arrow", "menu", "cart",
            "150x150", "100x100", "50x50",
        ]):
            continue
        if not await is_image_known(url):
            new_images.append(img)

    if not new_images:
        await log_source_scrape(source_url, source_name, 0, "success")
        return []

    # Download and encode images (limit to 10 per source to manage cost)
    downloaded = []
    for img in new_images[:15]:
        result = await download_and_encode_image(img["url"])
        if result:
            b64_data, media_type = result
            downloaded.append({
                "url": img["url"],
                "alt": img.get("alt", ""),
                "context": img.get("context", "")[:300],
                "b64": b64_data,
                "media_type": media_type,
            })
        if len(downloaded) >= 10:
            break

    if not downloaded:
        await log_source_scrape(source_url, source_name, len(new_images), "success")
        return []

    # Build vision message with actual images
    content_blocks = []

    content_blocks.append({
        "type": "text",
        "text": f"I'm showing you {len(downloaded)} images from {source_name} ({source_url}). "
                f"Please LOOK at each image and evaluate it for @tatamispaces.\n\n"
                f"For each image, I'll show the image followed by its metadata."
    })

    for i, img in enumerate(downloaded):
        # Add the actual image
        content_blocks.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": img["media_type"],
                "data": img["b64"],
            },
        })
        # Add metadata
        content_blocks.append({
            "type": "text",
            "text": f"Image {i+1} | URL: {img['url']}\nContext: {img['context'][:200]}"
        })

    content_blocks.append({
        "type": "text",
        "text": f"""Now evaluate ALL images above. For each one scoring 7+/10, include it.

Return ONLY a JSON array:
[
  {{
    "image_number": 1,
    "image_url": "the url from metadata",
    "source_url": "{source_url}",
    "source_name": "{source_name}",
    "title": "What you see in the image",
    "quality_score": 8,
    "scroll_stop_factor": "Why this stops the scroll",
    "curator_notes": "Your visual assessment"
  }}
]

If NOTHING scores 7+, return []. Be brutally selective."""
    })

    response = client.messages.create(
        model=CURATOR_MODEL,
        max_tokens=4096,
        system=build_curator_system_prompt(niche_id),
        messages=[{"role": "user", "content": content_blocks}],
    )

    # Parse response
    try:
        response_text = response.content[0].text

        json_start = response_text.find("[")
        json_end = response_text.rfind("]") + 1

        if json_start == -1 or json_end == 0:
            await log_source_scrape(source_url, source_name, len(downloaded), "success")
            return []

        json_str = response_text[json_start:json_end]
        results = json.loads(json_str)

        curated = []
        for item in results:
            curated.append(CuratedImage(
                image_url=item.get("image_url", ""),
                source_url=item.get("source_url", source_url),
                source_name=item.get("source_name", source_name),
                title=item.get("title"),
                description=item.get("description"),
                quality_score=item.get("quality_score", 7),
                curator_notes=item.get("curator_notes", ""),
                scroll_stop_factor=item.get("scroll_stop_factor", ""),
            ))

        await log_source_scrape(source_url, source_name, len(downloaded), "success")
        return curated

    except (json.JSONDecodeError, KeyError, IndexError):
        await log_source_scrape(source_url, source_name, len(downloaded), "partial")
        return []


async def find_images(
    niche_id: str,
    search_query: Optional[str] = None,
    source_url: Optional[str] = None,
    count: int = 5,
) -> list[CuratedImage]:
    """
    Main entry point: Find curated images for a niche.

    Args:
        niche_id: Which niche to curate for
        search_query: Custom search query (optional)
        source_url: Specific source URL to scrape (optional)
        count: Target number of images to find

    Returns:
        List of curated images that passed quality bar
    """
    curated_images = []

    if source_url:
        # Scrape specific source
        results = await evaluate_images_from_source(
            source_url,
            "Custom Source",
            niche_id,
        )
        curated_images.extend(results)

    elif search_query:
        # Web search for query
        search_results = await search_web_for_images(search_query, count * 2)

        for result in search_results:
            url = result.get("url")
            if url:
                results = await evaluate_images_from_source(
                    url,
                    result.get("title", "Web Search"),
                    niche_id,
                )
                curated_images.extend(results)
                if len(curated_images) >= count:
                    break

    else:
        # Use source library
        all_sources = (
            ARCHITECTURE_FIRMS +
            ENGLISH_PUBLICATIONS +
            JAPANESE_SOURCES +
            HOSPITALITY_SOURCES
        )

        # Shuffle and try sources until we have enough
        import random
        random.shuffle(all_sources)

        for source in all_sources:
            results = await evaluate_images_from_source(
                source["url"],
                source["name"],
                niche_id,
            )
            curated_images.extend(results)
            if len(curated_images) >= count:
                break

    # Store candidates in database
    for img in curated_images[:count]:
        await add_candidate(
            niche=niche_id,
            image_url=img.image_url,
            source_url=img.source_url,
            source_name=img.source_name,
            title=img.title,
            description=img.description,
            curator_notes=f"{img.curator_notes}\n\nScroll-stop factor: {img.scroll_stop_factor}",
            quality_score=img.quality_score,
        )

    return curated_images[:count]


async def curate_with_conversation(
    niche_id: str,
    user_request: str,
) -> str:
    """
    Have a conversational curation session with Opus.
    For more complex curation requests.
    """
    niche = get_niche(niche_id)

    response = client.messages.create(
        model=CURATOR_MODEL,
        max_tokens=4096,
        system=build_curator_system_prompt(niche_id),
        messages=[{"role": "user", "content": user_request}],
    )

    return response.content[0].text
