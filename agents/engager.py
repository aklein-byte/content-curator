"""
Engager Agent — Drafts replies and evaluates posts for engagement.
Uses Claude to generate contextual, on-brand replies.
Reads voice.md for style guidance.
"""

import os
import json
import logging
from pathlib import Path
from typing import Optional

from anthropic import Anthropic
from config.niches import get_niche

logger = logging.getLogger(__name__)

ENGAGER_MODEL = "claude-sonnet-4-20250514"

client = Anthropic()

# Load voice guide once
_voice_guide: Optional[str] = None


def _load_voice_guide(niche_id: str = "tatamispaces") -> str:
    """Load the voice/style guide for writing."""
    global _voice_guide
    if _voice_guide is not None:
        return _voice_guide

    # Try niche-specific voice file first, fall back to default
    voice_path = Path(__file__).parent.parent / "config" / f"voice-{niche_id}.md"
    if not voice_path.exists():
        voice_path = Path(__file__).parent.parent / "config" / "voice.md"

    if voice_path.exists():
        _voice_guide = voice_path.read_text()
    else:
        _voice_guide = ""
    return _voice_guide


def _build_evaluator_prompt(niche_id: str) -> str:
    """Build the system prompt for evaluating posts."""
    niche = get_niche(niche_id)
    engagement = niche.get("engagement", {})

    return f"""You evaluate X posts for the account {niche['handle']} ({niche['description']}).

Your job: decide if a post is worth engaging with.

Score each post 1-10 on relevance:
- 9-10: Perfect fit, high engagement potential, our audience would love this
- 7-8: Good fit, worth engaging with
- 5-6: Tangentially related, engage only if we need to fill quota
- 1-4: Not relevant, skip

Consider:
- Is this about {niche['description']}? Does it fit our niche?
- Does it have good imagery our audience would appreciate?
- Is the author someone worth building a relationship with?
- Is this post getting traction (likes, reposts)?
- Would engaging with this make us look knowledgeable, not spammy?
- Is it recent enough that a reply would still be seen?

Return your evaluation as JSON:
{{
  "relevance_score": 8,
  "should_engage": true,
  "reason": "Brief explanation",
  "suggested_actions": ["reply", "like"]
}}

Possible actions: reply, like, repost, follow
- Always suggest "like" if score >= 6
- Suggest "reply" if score >= 7 and we have something useful to add
- Suggest "repost" only for score 9-10 content that perfectly fits our brand
- Suggest "follow" if the author consistently posts great content in our niche"""


def _build_reply_prompt(niche_id: str) -> str:
    """Build the system prompt for drafting replies."""
    niche = get_niche(niche_id)
    engagement = niche.get("engagement", {})
    voice_guide = _load_voice_guide(niche_id)

    reply_voice = engagement.get("reply_voice", "Knowledgeable but casual.")

    return f"""You write replies for the X account {niche['handle']}.

## Voice & Style
{reply_voice}

{voice_guide if voice_guide else ''}

## Reply Guidelines

1. BE HUMAN — you're a person who likes this stuff. Sometimes that means a short genuine reaction ("this is where I'd want to spend a rainy afternoon"), sometimes a question, sometimes a small detail you know. Mix it up. Not every reply needs to teach something.

2. KEEP IT SHORT — 1-2 sentences max. This is a reply, not a post. No hashtags.

3. DON'T BE SYCOPHANTIC — no "incredible work!" or "this is amazing!". But genuine appreciation is fine. "This room" or "want to sit there" is real. "Stunning capture!" is not.

4. REFERENCE THE TEXT, NOT IMAGES — you only have the post text, NOT the images. NEVER make up visual details you can't see. Only reference what the text explicitly says. If the text is vague, react to the topic or ask a question.

5. DON'T EXPLAIN WHO YOU ARE — never say "as a design account" or "we curate". Just be a person talking.

6. VARY YOUR APPROACH — rotate between these:
   - Genuine short reaction ("want to live here")
   - A real question about what they posted
   - A small relevant detail or context (don't force it)
   - Simple appreciation without being generic

7. AVOID — delve, tapestry, vibrant, nestled, fostering, leveraging, resonates, testament, beacon, groundbreaking. No em-dashes. No "not just X, it's Y" pattern. No lecturing.

Return ONLY the reply text. No quotes, no explanation."""


def _build_original_post_prompt(niche_id: str) -> str:
    """Build the system prompt for drafting original posts from discovered content."""
    niche = get_niche(niche_id)
    voice_guide = _load_voice_guide(niche_id)

    return f"""You write original posts for the X account {niche['handle']}.

{voice_guide if voice_guide else ''}

## Task
You're given a source post (often in Japanese) with images. Write an original post that:

1. Downloads and reposts the images with credit (📷 @handle)
2. Adds context the English-speaking audience wouldn't know
3. Follows the voice guide exactly
4. Includes specific details: measurements, materials, costs, locations, architect names
5. Ends with the detail that sticks

## Format
Return JSON:
{{
  "text": "The post text including 📷 @credithandle at the end",
  "credit_handle": "originalauthor"
}}"""


async def evaluate_post(
    post_text: str,
    author: str,
    niche_id: str,
    image_count: int = 0,
    likes: int = 0,
    reposts: int = 0,
) -> dict:
    """
    Evaluate whether a discovered post is worth engaging with.

    Returns:
        {
            "relevance_score": 1-10,
            "should_engage": bool,
            "reason": str,
            "suggested_actions": ["reply", "like", ...]
        }
    """
    prompt = f"""Evaluate this post:

Author: @{author}
Text: {post_text}
Images: {image_count} attached
Engagement: {likes} likes, {reposts} reposts

Is this worth engaging with?"""

    try:
        response = client.messages.create(
            model=ENGAGER_MODEL,
            max_tokens=256,
            system=_build_evaluator_prompt(niche_id),
            messages=[{"role": "user", "content": prompt}],
        )

        text = response.content[0].text

        # Parse JSON from response
        json_start = text.find("{")
        json_end = text.rfind("}") + 1
        if json_start >= 0 and json_end > json_start:
            result = json.loads(text[json_start:json_end])
            return {
                "relevance_score": result.get("relevance_score", 5),
                "should_engage": result.get("should_engage", False),
                "reason": result.get("reason", ""),
                "suggested_actions": result.get("suggested_actions", []),
            }
    except Exception as e:
        logger.error(f"Failed to evaluate post by @{author}: {e}")

    return {
        "relevance_score": 5,
        "should_engage": False,
        "reason": "Evaluation failed",
        "suggested_actions": [],
    }


async def draft_reply(
    post_text: str,
    author: str,
    niche_id: str,
    image_description: Optional[str] = None,
) -> str:
    """
    Draft a reply to a post using the niche's voice and style.

    Returns:
        Draft reply text
    """
    prompt = f"""Write a reply to this post:

@{author}: {post_text}"""

    if image_description:
        prompt += f"\n\n[Images show: {image_description}]"

    prompt += "\n\nWrite the reply:"

    try:
        response = client.messages.create(
            model=ENGAGER_MODEL,
            max_tokens=280,
            system=_build_reply_prompt(niche_id),
            messages=[{"role": "user", "content": prompt}],
        )
        reply = response.content[0].text.strip()
        # Remove any wrapping quotes the model might add
        if reply.startswith('"') and reply.endswith('"'):
            reply = reply[1:-1]
        return reply
    except Exception as e:
        logger.error(f"Failed to draft reply to @{author}: {e}")
        return ""


async def draft_original_post(
    source_text: str,
    author: str,
    niche_id: str,
    image_description: Optional[str] = None,
) -> dict:
    """
    Draft an original post (download + repost with credit).

    Returns:
        {"text": str, "credit_handle": str}
    """
    prompt = f"""Source post from @{author}:
{source_text}"""

    if image_description:
        prompt += f"\n\nImages show: {image_description}"

    prompt += "\n\nWrite the original post with credit:"

    try:
        response = client.messages.create(
            model=ENGAGER_MODEL,
            max_tokens=512,
            system=_build_original_post_prompt(niche_id),
            messages=[{"role": "user", "content": prompt}],
        )

        text = response.content[0].text

        # Try to parse as JSON
        json_start = text.find("{")
        json_end = text.rfind("}") + 1
        if json_start >= 0 and json_end > json_start:
            result = json.loads(text[json_start:json_end])
            return {
                "text": result.get("text", ""),
                "credit_handle": result.get("credit_handle", author),
            }

        # Fallback: use the whole response as text
        return {
            "text": text.strip(),
            "credit_handle": author,
        }
    except Exception as e:
        logger.error(f"Failed to draft original post from @{author}: {e}")
        return {"text": "", "credit_handle": author}
