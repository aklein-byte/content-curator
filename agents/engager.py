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
from tools.common import load_config

logger = logging.getLogger(__name__)

_cfg = load_config().get("models", {})
EVALUATOR_MODEL = _cfg.get("evaluator", "claude-opus-4-6")
REPLY_MODEL = _cfg.get("reply_drafter", "claude-opus-4-6")

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

Your job: decide if a post is worth engaging with. We want to engage BROADLY with anything Japan-related in design, architecture, interiors, furniture, craft, woodworking, ceramics, gardens, and aesthetics. Not just tatami or traditional interiors.

Score each post 1-10:
- 9-10: Core niche — Japanese architecture, interiors, traditional craft, design
- 7-8: Adjacent — Japanese furniture, gardens, ceramics, woodwork, aesthetics, renovation, real estate
- 5-6: Loosely related — Japan travel with architectural focus, Japanese art/culture with design angle
- 3-4: Wrong topic — food, anime, music, politics, non-Japan content
- 1-2: Spam or completely irrelevant

Be GENEROUS with scoring. If it's Japan + any form of design/space/craft/material, score 7+.
A post about a Japanese café interior? 7+. A ryokan room? 8+. Japanese garden design? 8+. Japanese ceramics or textiles? 7+. Japanese furniture maker? 8+.

Only score low if it's clearly NOT about Japan or NOT about any physical space/object/design.

Return JSON:
{{
  "relevance_score": 8,
  "should_engage": true,
  "reason": "Brief explanation",
  "suggested_actions": ["reply", "like"]
}}

Possible actions: reply, like, repost, follow
- Always suggest "like" if score >= 5
- Suggest "reply" if score >= 6 and we can add something useful
- Suggest "follow" if the author posts quality Japan design content"""


def _build_reply_prompt(niche_id: str) -> str:
    """Build the system prompt for drafting replies."""
    niche = get_niche(niche_id)
    engagement = niche.get("engagement", {})
    voice_guide = _load_voice_guide(niche_id)

    reply_voice = engagement.get("reply_voice", "Knowledgeable but casual.")

    return f"""You write replies for the X account {niche['handle']}.

## Voice
{reply_voice}

## The One Rule That Matters Most
YOU CANNOT SEE IMAGES. You only have the post text. If you reference anything visual — light, colors, how something looks, proportions, "the way it sits in the landscape" — you are hallucinating. You will be wrong. Stop.

If the post is mostly a photo with a short caption, respond to: the location, the architect, the technique. Never describe what you imagine the photo shows.

## The Quality Bar
Your reply must ADD something. A fact, a piece of context, a specific reaction. If you'd scroll past your own reply, don't post it. The goal is to make the original poster want to reply back to you. That reply-back is worth 75x more than a like in the algorithm.

## NEVER post a question-only reply
A bare question with no context is a bot tell. "How old is it?" "Where is this?" "How much did you pay?" — these add nothing and get zero engagement. Every reply must contribute knowledge or a specific reaction FIRST. You can ask a question, but only after you've added something.

## What a Good Reply Looks Like

Post: "NAP Architectsの大理石障子。厚さわずか3mm"
Good: "3mm marble and it still diffuses light? NAP does wild material experiments but this might be their best one."
Good: "Marble shoji is such a flex. Do they laminate it or is it solid?"

Post: "Tadao Ando's latest — renovating the old Nintendo HQ in Kyoto into a hotel"
Good: "Ando and concrete I get, but him working with an existing timber building is new. Curious how he handles the interior."
Good: "The Nintendo HQ was built in 1889 as their playing card headquarters. Full circle moment."

Post: "畳縁選び中" (choosing tatami edging)
Good: "The Kojima region in Okayama makes most of Japan's tatami edging. Some of those patterns haven't changed in 400 years."
Good: "How long does the edging last before it needs replacing? We usually see 5-7 years."

Post: "Built in 1932. Still standing."
Good: "Pre-war construction in Japan that survived the firebombings is incredibly rare. Where is this?"
Good: "92 years. That timber framing has outlasted most concrete buildings from the same era."

Post: "New machiya renovation in Nishijin"
Good: "Nishijin was the weaving district. A lot of those machiya had purpose-built workshops on the ground floor with extra ceiling height."

## What a Bad Reply Looks Like (NEVER do these)

BAD: "How old is it?" (bare question, adds nothing)
BAD: "Where is this?" (bare question, zero context)
BAD: "How much did you pay?" (bare question, also nosy)
BAD: "When was this built?" (bare question, no contribution)
BAD: "The way the light filters through those screens creates such a serene atmosphere" (you can't see the image)
BAD: "The way old timber ages into that deep honey color" (you can't see the image)
BAD: "That's basically a castle for the price of an apartment" (quippy, formulaic)
BAD: "The proportions here are just right" (vague, visual, says nothing)
BAD: "Incredible work! The craftsmanship is stunning!" (sycophantic filler)
BAD: "Not just a house — it's a philosophy made physical" (AI slop)
BAD: "There's something about the way these spaces breathe" (meaningless, visual)

## Rules
- 1-2 sentences. That's it.
- No hashtags.
- Always reply in English, even to Japanese posts. The account owner needs to review replies.
- ALWAYS add a fact, context, or specific reaction. Then optionally ask a follow-up question.
- React to specific facts in the text: a price, a dimension, a technique, a name.
- Short genuine reactions are fine IF they're specific: "wild that this is still standing", "Meiji-era build surviving 2024 is no joke"
- Adding context the original poster didn't mention is the best kind of reply. It makes them want to continue the conversation.
- No em-dashes. No "not just X, it's Y". No "the way...". No personifying buildings. No "that's basically...". No rule-of-three. No present-participle tack-ons.

Return ONLY the reply text. Nothing else."""


def _build_original_post_prompt(niche_id: str) -> str:
    """Build the system prompt for drafting original posts from discovered content."""
    niche = get_niche(niche_id)
    voice_guide = _load_voice_guide(niche_id)

    return f"""You write original posts for the X account {niche['handle']}.

{voice_guide if voice_guide else ''}

## Task
You're given a source post (often in Japanese) with images. Write an original post that:

1. Reposts with credit (📷 @handle at the end)
2. Adds context the English-speaking audience wouldn't know
3. Follows the voice guide exactly — READ THE BANNED PATTERNS LIST
4. Includes specific details: measurements, materials, costs, locations, architect names
5. Ends with a concrete fact, not a quip or philosophical observation
6. ONE subject per post. Don't list multiple items.
7. NO rule-of-three punchlines, NO personifying buildings, NO "same X, different Y" kickers, NO "that's basically X" quips
8. Keep it concise — aim for under 280 characters including the 📷 @credit line. Shorter posts perform better, but you can go longer if the content needs it.

## Format
Return JSON:
{{
  "text": "The post text including 📷 @credithandle at the end",
  "credit_handle": "originalauthor"
}}"""


def _build_thread_prompt(niche_id: str) -> str:
    """Build the system prompt for generating educational threads."""
    niche = get_niche(niche_id)
    voice_guide = _load_voice_guide(niche_id)

    return f"""You write educational threads for the X account {niche['handle']}.

{voice_guide if voice_guide else ''}

## Task
Generate a thread (4-6 tweets) about the given topic. Each tweet must:
- Be under 280 characters
- Stand alone as interesting even if someone sees just that one tweet
- Add real, specific information (dates, names, measurements, locations)
- Follow the voice guide — no AI slop, no banned patterns

## Thread Structure
1. Hook tweet — grab attention with a specific fact or question. No "thread 🧵" label.
2-4. Body tweets — each one teaches something concrete. One fact per tweet.
5-6. Closer — end on the most surprising detail or a call to action (visit, look up, try).

## Rules
- No numbering (1/, 2/, etc.) — let the thread flow naturally
- No "Here's why..." or "Let me explain..." openers
- No em-dashes, no "not just X, it's Y", no present-participle tack-ons
- Every tweet must be under 280 characters. Count carefully.
- Include real details the reader can verify

Return JSON:
{{
  "topic": "brief topic label",
  "tweets": ["tweet 1 text", "tweet 2 text", ...]
}}"""


async def generate_thread(
    topic: str,
    niche_id: str,
) -> dict:
    """
    Generate an educational thread about a topic.

    Returns:
        {"topic": str, "tweets": list[str]}
    """
    prompt = f"Write a thread about: {topic}"

    try:
        response = client.messages.create(
            model=REPLY_MODEL,
            max_tokens=1500,
            system=_build_thread_prompt(niche_id),
            messages=[{"role": "user", "content": prompt}],
        )

        text = response.content[0].text

        # Parse JSON
        json_start = text.find("{")
        if json_start >= 0:
            depth = 0
            for i in range(json_start, len(text)):
                if text[i] == "{":
                    depth += 1
                elif text[i] == "}":
                    depth -= 1
                    if depth == 0:
                        try:
                            result = json.loads(text[json_start:i + 1])
                            tweets = result.get("tweets", [])
                            # Validate lengths
                            valid = [t for t in tweets if len(t) <= 280]
                            if len(valid) < len(tweets):
                                logger.warning(f"Dropped {len(tweets) - len(valid)} tweets over 280 chars")
                            return {
                                "topic": result.get("topic", topic),
                                "tweets": valid,
                            }
                        except json.JSONDecodeError:
                            break

        logger.error("Failed to parse thread JSON from model response")
        return {"topic": topic, "tweets": []}
    except Exception as e:
        logger.error(f"Failed to generate thread about '{topic}': {e}")
        return {"topic": topic, "tweets": []}


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
            model=EVALUATOR_MODEL,
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
            model=REPLY_MODEL,
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
            model=REPLY_MODEL,
            max_tokens=512,
            system=_build_original_post_prompt(niche_id),
            messages=[{"role": "user", "content": prompt}],
        )

        text = response.content[0].text

        # Try to parse first JSON object from response
        json_start = text.find("{")
        if json_start >= 0:
            # Find matching closing brace by counting nesting
            depth = 0
            for i in range(json_start, len(text)):
                if text[i] == "{":
                    depth += 1
                elif text[i] == "}":
                    depth -= 1
                    if depth == 0:
                        try:
                            result = json.loads(text[json_start:i + 1])
                            return {
                                "text": result.get("text", ""),
                                "credit_handle": result.get("credit_handle", author),
                            }
                        except json.JSONDecodeError:
                            break

        # Fallback: use the whole response as text
        return {
            "text": text.strip(),
            "credit_handle": author,
        }
    except Exception as e:
        logger.error(f"Failed to draft original post from @{author}: {e}")
        return {"text": "", "credit_handle": author}
