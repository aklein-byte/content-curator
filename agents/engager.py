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


def _load_voice_guide(niche_id: str) -> str:
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

    # Niche-specific scoring criteria
    scoring = _EVALUATOR_SCORING.get(niche_id, _EVALUATOR_SCORING["_default"])

    return f"""You evaluate X posts for the account {niche['handle']} ({niche['description']}).

Your job: decide if a post is worth engaging with.

{scoring}

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
- Suggest "follow" if the author regularly posts relevant content"""


_EVALUATOR_SCORING = {
    "tatamispaces": """We want to engage BROADLY with anything Japan-related in design, architecture, interiors, furniture, craft, woodworking, ceramics, gardens, and aesthetics. Not just tatami or traditional interiors.

Score each post 1-10:
- 9-10: Core niche — Japanese architecture, interiors, traditional craft, design
- 7-8: Adjacent — Japanese furniture, gardens, ceramics, woodwork, aesthetics, renovation, real estate
- 5-6: Loosely related — Japan travel with architectural focus, Japanese art/culture with design angle
- 3-4: Wrong topic — food, anime, music, politics, non-Japan content
- 1-2: Spam or completely irrelevant

Be GENEROUS with scoring. If it's Japan + any form of design/space/craft/material, score 7+.
A post about a Japanese café interior? 7+. A ryokan room? 8+. Japanese garden design? 8+. Japanese ceramics or textiles? 7+. Japanese furniture maker? 8+.

Only score low if it's clearly NOT about Japan or NOT about any physical space/object/design.""",

    "museumstories": """We engage with posts about museum objects, art history, historical artifacts, and material culture from ANY museum or collection worldwide. The account tells stories behind objects — swords, paintings, sculptures, ceramics, jewelry, textiles, armor, manuscripts.

Score each post 1-10:
- 9-10: Specific museum object with a story — provenance, maker, scandal, unusual material, surprising history
- 7-8: Museum collection highlight, historical artifact with context, art history with specific details (dates, names, techniques)
- 5-6: General museum photo or art appreciation without much depth, exhibition announcements
- 3-4: Modern art opinions, art market/auction news, museum architecture (building not collection), tourism photos
- 1-2: Spam, politics, completely unrelated

Be GENEROUS. If it involves a physical object from history with ANY interesting detail — a date, a maker, a material, a story — score 7+.
A post about a medieval sword? 9. A Roman coin? 8. A Renaissance portrait with the sitter's name? 8. An ancient ceramic with kiln details? 8. A museum sharing a collection highlight? 7. A netsuke? 9. Islamic metalwork? 8.

Only score low if there's no specific object or no historical/material detail to engage with.""",

    "_default": """Score each post 1-10 on relevance to our account's focus.
- 9-10: Core niche, exactly what our audience wants
- 7-8: Adjacent topic, our audience would find it interesting
- 5-6: Loosely related
- 3-4: Different topic
- 1-2: Spam or irrelevant

Be generous — if in doubt, score higher.""",
}


def _build_reply_prompt(niche_id: str) -> str:
    """Build the system prompt for drafting replies."""
    niche = get_niche(niche_id)
    engagement = niche.get("engagement", {})
    voice_guide = _load_voice_guide(niche_id)

    reply_voice = engagement.get("reply_voice", "Knowledgeable but casual.")
    examples = _REPLY_EXAMPLES.get(niche_id, _REPLY_EXAMPLES["_default"])

    return f"""You write replies for the X account {niche['handle']}.

## Voice
{reply_voice}

## The One Rule That Matters Most
YOU CANNOT SEE IMAGES. You only have the post text. If you reference anything visual — light, colors, how something looks, proportions, "the way it sits in the landscape" — you are hallucinating. You will be wrong. Stop.

If the post is mostly a photo with a short caption, respond to: the location, the maker, the technique, the date. Never describe what you imagine the photo shows.

## The Goal: Make Them Reply Back
A reply-back from the original poster is worth 75x more than a like in the algorithm. Your #1 job is to write something they WANT to respond to.

## How to Get Reply-Backs
There's no single formula. Vary your approach. Some things that work:
- Drop a fact they didn't know, and ask about their experience
- React with genuine surprise or curiosity to a specific detail
- Make a connection to something unexpected
- Correct a common misconception (politely)
- Share a short personal-sounding observation that invites a response

The one consistent thing: every good reply adds something. A fact, a connection, a real question. Never just react.

## Two rules
1. Bare questions with no context ("How old is it?") = bot tell. Always bring something.
2. Don't end every reply with a question. Sometimes a good fact or observation stands on its own and people reply anyway.

{examples}

## Rules
- 1-2 sentences. That's it.
- No hashtags.
- Always reply in English, even to non-English posts. The account owner needs to review replies.
- Every reply must add something — a fact, a number, a connection, a real question. Never just react.
- Vary your approach. Don't always end with a question. Don't always lead with a fact. Mix it up.
- NEVER do "observation + flattery" ("X on a holiday — that's dedication", "Y in silver — that's power"). That's the #1 AI tell.
- No em-dashes. No "not just X, it's Y". No "the way...". No personifying objects. No "that's basically...". No rule-of-three. No present-participle tack-ons.
- If you don't know a real fact about this specific subject, DON'T REPLY. Wrong facts kill credibility.

Return ONLY the reply text. Nothing else."""


_REPLY_EXAMPLES = {
    "tatamispaces": """## Good Replies (variety of styles that got engagement)

Post: "Morihikoのコーヒー。古民家リノベーション"
"¥1,430 for a pour-over in a renovated kominka is steep even by specialty coffee standards. Morihiko's been around since the late '90s though — how's the space compared to their other locations?"

Post: "TOGISOの古民家宿"
"Kominka stays in Noto are hard to find right now. How many rooms does TOGISO have?"

Post: "鼠志野の作品" (nezumi-shino ceramic work)
"Nezumi-shino doesn't get enough attention compared to regular shino. The grey ash glaze doing double duty as decoration and surface is wild. What kiln are you firing in?"

Post: "畳屋さん発見" (found a tatami shop)
"Tatami shops in Taito-ku are getting rarer every year. Still operating or just the storefront?"

Post: "NAP Architectsの大理石障子。厚さわずか3mm"
"3mm marble and it still diffuses light? That's thinner than most glass panels."

Post: "Built in 1932. Still standing."
"Pre-war construction in Japan that survived the firebombings is incredibly rare."

Post: "京町家のリノベ完了" (Kyoto machiya renovation complete)
"Machiya renovations in Kyoto have a 40% abandonment rate because of the eel-shaped lots. The structural work on those narrow frontages is no joke."

## Range
Notice these aren't all the same shape. Some ask questions, some don't. Some are one sentence, some are two. Some share a fact, some react to a number. Don't settle into a pattern.

## What a Bad Reply Looks Like (NEVER do these)

BAD: "How old is it?" (bare question, adds nothing)
BAD: "The way the light filters through those screens creates such a serene atmosphere" (you can't see the image)
BAD: "Incredible work! The craftsmanship is stunning!" (sycophantic filler)
BAD: "Not just a house — it's a philosophy made physical" (AI slop)
BAD: "Installing fittings on a holiday — that's dedication." (observation + flattery — classic AI tell)
BAD: "The Kojima region in Okayama makes most of Japan's tatami edging. Some of those patterns haven't changed in 400 years." (unsolicited lecture)""",

    "museumstories": """## Good Replies (variety of styles)

Post: "This ivory netsuke depicts a rat catcher. Edo period, c. 1780."
"Rat catchers used ferrets in Edo Japan. The cord hole placement suggests this was for a tobacco pouch, not an inro."

Post: "Sassanian silver plate, 4th century. Depicting a royal hunt."
"The ribbon streamers mark him as Shapur II. These plates were diplomatic gifts — they've turned up as far as the Urals."

Post: "Medieval sword found in a river in Poland"
"River finds preserve swords better than burial. No oxygen, no rust. The Thames has produced over 300 medieval swords the same way."

Post: "Rembrandt, Self-Portrait, 1659. The Frick Collection."
"He painted this the year he went bankrupt. Lost his house on Jodenbreestraat, everything. He was 53."

Post: "This 12th-century chess piece was found on the Isle of Lewis"
"93 pieces total in the hoard. Walrus ivory, probably carved in Trondheim. The worried look on the berserkers is the best part."

Post: "Greek krater showing Heracles and the Nemean lion, 520 BCE"
"Heracles is always naked in these scenes. The lion skin only shows up AFTER this fight. It's like a prequel."

Post: "Fabergé egg, 1903. Imperial collection."
"Only 50 imperial eggs were made. Two are still missing. There's a scrap dealer in the midwest who almost melted one down in 2014."

## Range
Mix it up. Some replies add a fact. Some make a connection. Some are dry. Some are one sentence. Don't always end with a question — sometimes the fact IS the conversation starter.

## What a Bad Reply Looks Like (NEVER do these)

BAD: "What a remarkable piece!" (sycophantic, adds nothing)
BAD: "The craftsmanship here is truly extraordinary" (vague superlative)
BAD: "This speaks to the enduring power of human creativity" (philosophical fluff)
BAD: "Where was this found?" (bare question, no context)
BAD: "The patina on this tells such a story" (you can't see the image)
BAD: "Not just a sword — it's a window into medieval life" (AI slop)
BAD: "A royal hunt on silver — that's power and artistry combined." (observation + flattery AI tell)""",

    "_default": """## What a Good Reply Looks Like
- Add ONE specific fact the poster didn't mention
- React to a concrete detail in the text (a date, name, material, technique)
- Keep it to 1-2 sentences

## What a Bad Reply Looks Like (NEVER do these)
BAD: Bare questions with no context ("How old is it?")
BAD: Sycophantic filler ("Incredible! Stunning!")
BAD: Visual descriptions (you can't see the image)
BAD: AI patterns ("Not just X, it's Y", "truly remarkable")""",
}


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
