"""
Respond to replies on @tatamispaces posts.

Checks mentions via official X API v2, evaluates which replies deserve a response,
drafts and posts responses to the top ones. Reinforces conversation and
boosts algorithmic distribution.

Usage: python respond.py [--niche tatamispaces] [--dry-run] [--max-responses 5]
"""

import sys
import os
import json
import asyncio
import argparse
from pathlib import Path
from datetime import datetime, timezone, timedelta

sys.path.insert(0, str(Path(__file__).parent))

from dotenv import load_dotenv
load_dotenv()

from tools.xapi import get_mentions, reply_to_post, _get_user_id, set_niche as set_xapi_niche
from tools.common import load_json, save_json, random_delay, acquire_lock, release_lock, setup_logging, load_config
from config.niches import get_niche
from anthropic import Anthropic

log = setup_logging("respond")

BASE_DIR = Path(__file__).parent
RESPONSE_LOG = Path(os.environ.get("RESPONSE_LOG", str(BASE_DIR / "response-log.json")))
SINCE_ID_FILE = BASE_DIR / "data" / "respond-since-id.txt"

MAX_RESPONSES_PER_RUN = 5
_delays = load_config().get("delays", {}).get("respond", [30, 90])
DELAY_MIN = _delays[0]
DELAY_MAX = _delays[1]

anthropic = Anthropic()
_models = load_config().get("models", {})


def already_responded_to_tweet(log_entries: list, tweet_id: str) -> bool:
    return any(e.get("reply_to_tweet_id") == tweet_id for e in log_entries)


def _build_response_prompt(niche_id: str) -> str:
    niche = get_niche(niche_id)
    voice_path = BASE_DIR / "config" / f"voice-{niche_id}.md"
    if not voice_path.exists():
        voice_path = BASE_DIR / "config" / "voice.md"
    voice_guide = voice_path.read_text() if voice_path.exists() else ""

    return f"""You write responses to people who reply to {niche['handle']}'s posts on X.

## Context
You are responding AS {niche['handle']}. Someone replied to one of your posts. You want to:
- Acknowledge good comments warmly but briefly
- Answer questions with specific knowledge
- Keep conversation going naturally
- Sound like a real person, not a brand

## Voice
{voice_guide if voice_guide else 'Knowledgeable, casual, specific. No AI slop.'}

## Rules
- 1-2 sentences max. Shorter is better.
- Match the energy of the reply. Enthusiastic reply? Match it. Technical question? Give a real answer.
- If someone asks a question you don't know the answer to, say so honestly. "Good question, not sure" is fine.
- If the reply is just "wow" or "amazing" — a simple "thanks!" or "glad you like it" is enough. Don't over-reply.
- If someone adds useful info (correction, additional context), acknowledge it genuinely.
- No hashtags. No emojis unless the replier used them.
- Always respond in English.
- No em-dashes. No "not just X, it's Y". No present-participle tack-ons.
- Never be defensive. If someone disagrees, engage thoughtfully or let it go.
- Don't repeat information from your original post.
- NEVER ask questions back. Don't end with "How did you...?", "Do you know...?", "What about...?", etc. Just make a statement. Asking questions on your own posts looks weird and artificial.

## What Good Responses Look Like

Reply: "How thick is the marble?"
Good response: "3mm. Wild that it doesn't crack."

Reply: "This is in Kyoto right?"
Good response: "Karuizawa actually. About 2 hours from Tokyo by train."

Reply: "Incredible craftsmanship"
Good response: "Right? Took the team 8 months."

Reply: "I think this is actually by Kengo Kuma, not Ando"
Good response: "You're right, my mistake. Thanks for the correction."

Reply: "🔥🔥🔥"
Good response: "🙏"

Return ONLY the response text. Nothing else."""


def _build_eval_prompt(niche_id: str) -> str:
    niche = get_niche(niche_id)
    return f"""You evaluate replies to {niche['handle']}'s posts on X.

Decide if each reply deserves a response. Score 1-10:
- 9-10: Asked a specific question, added valuable info, or made a thoughtful comment. Respond.
- 7-8: Genuine engagement, worth a quick response. Respond.
- 5-6: Generic positive ("amazing!"). Maybe respond if it's early or from a notable account.
- 3-4: Low-effort ("🔥", single emoji). Skip unless it's the only reply.
- 1-2: Spam, bot, unrelated. Skip.

Prioritize:
- Questions (always respond to real questions)
- Replies that add new information or corrections
- Replies in a foreign language that ask something (respond in English)

Return JSON:
{{
  "score": 8,
  "should_respond": true,
  "reason": "Brief explanation",
  "priority": "high"
}}

priority: "high" (questions, corrections), "medium" (genuine engagement), "low" (generic praise)"""


async def evaluate_reply(our_post_text: str, reply_text: str, replier_handle: str, niche_id: str) -> dict:
    prompt = f"""Our post: {our_post_text}

Reply from @{replier_handle}: {reply_text}

Should we respond?"""

    try:
        response = anthropic.messages.create(
            model=_models.get("evaluator", "claude-sonnet-4-20250514"),
            max_tokens=200,
            system=_build_eval_prompt(niche_id),
            messages=[{"role": "user", "content": prompt}],
        )
        text = response.content[0].text
        json_start = text.find("{")
        json_end = text.rfind("}") + 1
        if json_start >= 0 and json_end > json_start:
            result = json.loads(text[json_start:json_end])
            return {
                "score": result.get("score", 5),
                "should_respond": result.get("should_respond", False),
                "reason": result.get("reason", ""),
                "priority": result.get("priority", "medium"),
            }
    except Exception as e:
        log.error(f"Failed to evaluate reply from @{replier_handle}: {e}")

    return {"score": 5, "should_respond": False, "reason": "Evaluation failed", "priority": "low"}


async def draft_response(our_post_text: str, reply_text: str, replier_handle: str, niche_id: str) -> str:
    prompt = f"""Your original post: {our_post_text}

@{replier_handle} replied: {reply_text}

Write your response:"""

    try:
        response = anthropic.messages.create(
            model=_models.get("reply_drafter", "claude-opus-4-6"),
            max_tokens=280,
            system=_build_response_prompt(niche_id),
            messages=[{"role": "user", "content": prompt}],
        )
        text = response.content[0].text.strip()
        if text.startswith('"') and text.endswith('"'):
            text = text[1:-1]
        return text
    except Exception as e:
        log.error(f"Failed to draft response to @{replier_handle}: {e}")
        return ""


def load_since_id() -> str | None:
    """Load the last processed mention ID to avoid re-processing."""
    if SINCE_ID_FILE.exists():
        return SINCE_ID_FILE.read_text().strip() or None
    return None


def save_since_id(since_id: str):
    SINCE_ID_FILE.parent.mkdir(parents=True, exist_ok=True)
    SINCE_ID_FILE.write_text(since_id)


async def main():
    parser = argparse.ArgumentParser(description="Respond to replies on our posts")
    parser.add_argument("--niche", default="tatamispaces", help="Niche ID")
    parser.add_argument("--dry-run", action="store_true", help="Evaluate only, no actions")
    parser.add_argument("--max-responses", type=int, default=MAX_RESPONSES_PER_RUN, help="Max responses per run")
    args = parser.parse_args()

    niche_id = args.niche
    dry_run = args.dry_run
    max_responses = args.max_responses
    niche = get_niche(niche_id)
    set_xapi_niche(niche_id)
    handle = niche["handle"].lstrip("@")

    log.info(f"Checking replies for @{handle} ({'DRY RUN' if dry_run else 'LIVE'})")

    response_log = load_json(RESPONSE_LOG)
    if not isinstance(response_log, list):
        response_log = []

    # Get recent mentions via official API
    since_id = load_since_id()
    mentions = get_mentions(max_results=50, since_id=since_id)

    if not mentions:
        log.info("No new mentions found.")
        log.info("Done. Responses: 0")
        return

    log.info(f"Found {len(mentions)} new mentions")

    # Track highest ID for next run
    highest_id = max(m.tweet_id for m in mentions)

    # Filter: only replies to OUR tweets (not random @mentions)
    our_user_id = _get_user_id()
    candidates = []
    for m in mentions:
        # Skip if no parent tweet (not a reply)
        if not m.parent_tweet_id:
            continue

        # Skip if already responded
        if already_responded_to_tweet(response_log, m.tweet_id):
            continue

        # Skip old mentions (> 48 hours)
        try:
            mention_time = datetime.fromisoformat(m.created_at.replace("Z", "+00:00"))
            if datetime.now(timezone.utc) - mention_time > timedelta(hours=48):
                continue
        except Exception:
            pass

        candidates.append({
            "tweet_id": m.tweet_id,
            "reply_text": m.text,
            "replier": m.author_handle,
            "parent_id": m.parent_tweet_id,
            "parent_text": m.parent_text or "[our post]",
            "created_at": m.created_at,
        })

    log.info(f"Found {len(candidates)} reply candidates to evaluate")

    if not candidates:
        # Still save since_id so we don't re-fetch these
        if not dry_run:
            save_since_id(highest_id)
        log.info("No reply candidates to respond to.")
        log.info("Done. Responses: 0")
        return

    # Evaluate each reply
    evaluated = []
    for c in candidates:
        eval_result = await evaluate_reply(
            c["parent_text"], c["reply_text"], c["replier"], niche_id,
        )
        c["eval"] = eval_result
        log.info(f"@{c['replier']} — score {eval_result['score']}/10 ({eval_result['reason'][:50]})")
        evaluated.append(c)

    # Sort by priority and score
    priority_order = {"high": 0, "medium": 1, "low": 2}
    evaluated.sort(key=lambda x: (priority_order.get(x["eval"]["priority"], 2), -x["eval"]["score"]))

    # Respond to top candidates
    responses_done = 0
    for c in evaluated:
        if responses_done >= max_responses:
            break

        if not c["eval"]["should_respond"]:
            log.info(f"Skip @{c['replier']} — score too low ({c['eval']['score']})")
            continue

        if dry_run:
            log.info(f"[DRY RUN] Would respond to @{c['replier']}: \"{c['reply_text'][:60]}...\"")
            responses_done += 1
            continue

        # Draft response
        response_text = await draft_response(
            c["parent_text"], c["reply_text"], c["replier"], niche_id,
        )
        if not response_text:
            log.warning(f"Empty response draft for @{c['replier']}, skipping")
            continue

        log.info(f"Responding to @{c['replier']}: \"{response_text[:80]}\"")

        if responses_done > 0:
            await random_delay("responding", DELAY_MIN, DELAY_MAX)

        # Post the response via official API
        reply_id = reply_to_post(c["tweet_id"], response_text)
        if reply_id:
            responses_done += 1
            log.info(f"Responded to @{c['replier']} ({responses_done}/{max_responses})")

            response_log.append({
                "reply_to_tweet_id": c["tweet_id"],
                "our_response_id": reply_id,
                "replier": c["replier"],
                "reply_text": c["reply_text"],
                "our_response": response_text,
                "parent_id": c.get("parent_id"),
                "score": c["eval"]["score"],
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "action": "respond",
            })
            save_json(RESPONSE_LOG, response_log)
        else:
            log.warning(f"Failed to post response to @{c['replier']}")

    # Save since_id so next run starts from where we left off
    if not dry_run:
        save_since_id(highest_id)

    log.info(f"Done. Responses: {responses_done}")


if __name__ == "__main__":
    lock_fd = acquire_lock(BASE_DIR / ".respond.lock")
    if not lock_fd:
        print("Another respond instance is running. Skipping.")
        sys.exit(0)
    try:
        asyncio.run(main())
    finally:
        release_lock(lock_fd)
