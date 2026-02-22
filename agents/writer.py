"""
Writer Agent - Generates captions for curated content.
Uses Claude Sonnet for efficient caption writing.
Caption writing is not the bottleneck - curation is.
"""

import os
import base64
import logging
from pathlib import Path
from typing import Optional

from config.niches import get_niche
from tools.common import load_config, get_anthropic, load_voice_guide

logger = logging.getLogger(__name__)

_cfg = load_config().get("models", {})

WRITER_MODEL = _cfg.get("writer", "claude-opus-4-6")

client = get_anthropic()


def build_writer_system_prompt(niche_id: str) -> str:
    """Build the system prompt for the writer agent."""
    niche = get_niche(niche_id)

    hashtags = " ".join(niche.get("hashtags", [])[:5])

    voice_guide = load_voice_guide(niche_id)

    return f"""You are the caption writer for {niche['name']} ({niche['handle']}).

{niche['writer_prompt']}

## Voice & Style Guide
{voice_guide if voice_guide else ''}

## Guidelines

1. KEEP IT SHORT — 1-4 sentences. No hashtags. Credit source with 📷 @handle.

2. SPECIFIC OVER GENERAL — real numbers, real names, real places. "3mm marble" not "innovative material."

3. ONE SUBJECT PER POST — don't list multiple things.

4. END WITH THE FACT THAT STICKS — a number, a cost, a surprising detail. Not a quip or philosophical observation.

5. AVOID — see the full banned list in the voice guide above. Key ones: no rule-of-three punchlines, no personifying buildings, no formulaic kickers ("same X, different Y"), no "that's basically X" quips, no present-participle tack-ons, no literary flourishes ("the kind of X where Y").
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


# Use Opus for vision-based thread captions — better at following voice rules
VISION_MODEL = _cfg.get("vision", "claude-opus-4-6")

THREAD_CAPTION_SYSTEM = """You're @tatamispaces. You post Japanese architecture and design.

Rules:
- Describe what's IN this specific image — what room, what detail, what material
- Add one fact the viewer can't see: dimension, cost, age, architect, technique
- Max 280 characters
- Short sentences. Fragments ok.
- No hashtags. No emojis unless the photo earns it.
- Don't repeat what tweet 1 already said
- Don't say "in this image" or "here we see" — just describe it
- No AI slop: no "tapestry", "vibrant", "nestled", "testament to", "delve", "landscape"
- No "not just X, it's Y" patterns
- No teaching or lecturing. Describe what's there.
- End with the detail that sticks — a number, a fact, a thing that surprises.
- Credit line is already handled — don't add 📷."""


def _image_to_base64(image_path: str) -> tuple[str, str]:
    """Read an image file and return (base64_data, media_type)."""
    path = Path(image_path)
    data = path.read_bytes()

    ext = path.suffix.lower()
    media_types = {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".gif": "image/gif",
        ".webp": "image/webp",
    }
    media_type = media_types.get(ext, "image/jpeg")

    # Resize if >5MB (Anthropic API limit)
    if len(data) > 5_000_000:
        try:
            from PIL import Image
            import io
            img = Image.open(io.BytesIO(data))
            if img.mode == "RGBA":
                img = img.convert("RGB")
            img.thumbnail((1920, 1920), Image.LANCZOS)
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=85)
            data = buf.getvalue()
            media_type = "image/jpeg"
        except ImportError:
            logger.warning("Pillow not installed, can't resize large image")

    return base64.b64encode(data).decode("utf-8"), media_type


async def generate_thread_captions(
    main_caption: str,
    image_paths: list[str],
    niche_id: str,
) -> list[str]:
    """
    Generate per-image captions for a thread post.

    Tweet 1 keeps the original caption. For each subsequent image,
    Claude Opus Vision generates a follow-up caption.

    Args:
        main_caption: The original post caption (used for tweet 1)
        image_paths: List of local image file paths (one per tweet)
        niche_id: Niche identifier

    Returns:
        List of captions — index 0 is main_caption, rest are generated.
    """
    if len(image_paths) <= 1:
        return [main_caption]

    captions = [main_caption]
    n = len(image_paths)

    for i, img_path in enumerate(image_paths[1:], start=2):
        try:
            img_b64, media_type = _image_to_base64(img_path)

            response = client.messages.create(
                model=VISION_MODEL,
                max_tokens=300,
                system=THREAD_CAPTION_SYSTEM,
                messages=[{
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": media_type,
                                "data": img_b64,
                            },
                        },
                        {
                            "type": "text",
                            "text": (
                                f"Tweet {i} of {n} in a thread.\n"
                                f"Tweet 1 said: \"{main_caption[:200]}\"\n"
                                f"Write the follow-up tweet for this image."
                            ),
                        },
                    ],
                }],
            )

            caption = response.content[0].text.strip()
            # Enforce 280 char limit
            if len(caption) > 280:
                caption = caption[:277] + "..."
            captions.append(caption)
            logger.info(f"Thread caption {i}/{n}: {caption[:80]}...")

        except Exception as e:
            logger.error(f"Vision caption failed for image {i}/{n}: {e}")
            captions.append(f"({i}/{n})")

    return captions
