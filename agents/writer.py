"""
Writer Agent - Generates captions for curated content.
Uses Claude Sonnet for efficient caption writing.
Caption writing is not the bottleneck - curation is.
"""

import os
from anthropic import Anthropic
from typing import Optional

from config.niches import get_niche

# Use Sonnet for writing - efficient and good enough
WRITER_MODEL = "claude-sonnet-4-20250514"

client = Anthropic()


def build_writer_system_prompt(niche_id: str) -> str:
    """Build the system prompt for the writer agent."""
    niche = get_niche(niche_id)

    hashtags = " ".join(niche.get("hashtags", [])[:5])

    return f"""You are the caption writer for {niche['name']} ({niche['handle']}).

{niche['writer_prompt']}

## Available Hashtags
{hashtags}

## Guidelines

1. KEEP IT SHORT
   - 1-3 sentences max
   - X has a 280 character limit
   - Leave room for hashtags

2. FOCUS ON VALUE
   - Educate or inspire, never just describe
   - Share the "why" not just the "what"
   - Connect to broader concepts

3. CREDIT SOURCES
   - If architect/designer is known, credit them
   - Link back to source when appropriate

4. HASHTAG SPARINGLY
   - 2-3 relevant hashtags max
   - No hashtag spam

5. AVOID
   - Emoji overuse
   - Clickbait
   - Generic statements
   - "Check out this..."
   - Starting with "This is..."
"""


async def write_caption(
    niche_id: str,
    image_context: str,
    source_name: Optional[str] = None,
    curator_notes: Optional[str] = None,
) -> dict:
    """
    Generate a caption for a curated image.

    Args:
        niche_id: Which niche this is for
        image_context: Description/context of the image
        source_name: Where the image came from
        curator_notes: Notes from the curator about why this image was selected

    Returns:
        dict with 'caption' and 'hashtags'
    """
    niche = get_niche(niche_id)

    prompt = f"""Write a caption for this image:

Context: {image_context}
"""

    if source_name:
        prompt += f"\nSource: {source_name}"

    if curator_notes:
        prompt += f"\nCurator notes: {curator_notes}"

    prompt += """

Return your response as:
CAPTION: [your caption here]
HASHTAGS: [2-3 hashtags, space-separated]
"""

    response = client.messages.create(
        model=WRITER_MODEL,
        max_tokens=512,
        system=build_writer_system_prompt(niche_id),
        messages=[{"role": "user", "content": prompt}],
    )

    response_text = response.content[0].text

    # Parse response
    caption = ""
    hashtags = []

    for line in response_text.split("\n"):
        if line.startswith("CAPTION:"):
            caption = line.replace("CAPTION:", "").strip()
        elif line.startswith("HASHTAGS:"):
            hashtag_text = line.replace("HASHTAGS:", "").strip()
            hashtags = [h.strip() for h in hashtag_text.split() if h.startswith("#")]

    # If parsing failed, use the whole response as caption
    if not caption:
        caption = response_text.split("\n")[0][:250]

    # Ensure we have some hashtags
    if not hashtags:
        hashtags = niche.get("hashtags", [])[:2]

    return {
        "caption": caption,
        "hashtags": hashtags,
    }


async def batch_write_captions(
    niche_id: str,
    images: list[dict],
) -> list[dict]:
    """
    Generate captions for multiple images.

    Args:
        niche_id: Which niche this is for
        images: List of dicts with 'context', 'source_name', 'curator_notes'

    Returns:
        List of dicts with 'caption' and 'hashtags'
    """
    results = []

    for image in images:
        result = await write_caption(
            niche_id=niche_id,
            image_context=image.get("context", image.get("title", "")),
            source_name=image.get("source_name"),
            curator_notes=image.get("curator_notes"),
        )
        results.append(result)

    return results


async def rewrite_caption(
    niche_id: str,
    original_caption: str,
    feedback: str,
) -> dict:
    """
    Rewrite a caption based on feedback.

    Args:
        niche_id: Which niche this is for
        original_caption: The caption to improve
        feedback: What should be changed

    Returns:
        dict with 'caption' and 'hashtags'
    """
    prompt = f"""Here's a caption that needs revision:

Original: {original_caption}

Feedback: {feedback}

Please rewrite the caption addressing this feedback.

Return your response as:
CAPTION: [your revised caption here]
HASHTAGS: [2-3 hashtags, space-separated]
"""

    response = client.messages.create(
        model=WRITER_MODEL,
        max_tokens=512,
        system=build_writer_system_prompt(niche_id),
        messages=[{"role": "user", "content": prompt}],
    )

    response_text = response.content[0].text

    # Parse response
    caption = ""
    hashtags = []

    for line in response_text.split("\n"):
        if line.startswith("CAPTION:"):
            caption = line.replace("CAPTION:", "").strip()
        elif line.startswith("HASHTAGS:"):
            hashtag_text = line.replace("HASHTAGS:", "").strip()
            hashtags = [h.strip() for h in hashtag_text.split() if h.startswith("#")]

    if not caption:
        caption = response_text.split("\n")[0][:250]

    niche = get_niche(niche_id)
    if not hashtags:
        hashtags = niche.get("hashtags", [])[:2]

    return {
        "caption": caption,
        "hashtags": hashtags,
    }
