"""
Respond to replies on @tatamispaces posts.

Checks mentions via official X API v2, evaluates which replies deserve a response,
drafts and posts responses to the top ones. Reinforces conversation and
boosts algorithmic distribution.

Usage: python respond.py [--niche tatamispaces] [--dry-run] [--max-responses 5]
"""

import sys
import json
import asyncio
import argparse
from pathlib import Path
from datetime import datetime, timezone, timedelta

sys.path.insert(0, str(Path(__file__).parent))

from dotenv import load_dotenv
load_dotenv()

from tools.xapi import get_mentions, reply_to_post, set_niche as set_xapi_niche
from tools.common import random_delay, setup_logging, load_config, get_anthropic, load_voice_guide, get_model, parse_json_response
from tools.db import log_engagement, already_engaged as db_already_engaged, get_db, acquire_process_lock, release_process_lock
from config.niches import get_niche

log = setup_logging("respond")

BASE_DIR = Path(__file__).parent
_niche_id: str = "tatamispaces"  # resolved in main()

MAX_RESPONSES_PER_RUN = 5
_delays = load_config().get("delays", {}).get("respond", [30, 90])
DELAY_MIN = _delays[0]
DELAY_MAX = _delays[1]

anthropic = get_anthropic()


def already_responded_to_tweet(tweet_id: str) -> bool:
    """Check if we already responded to this tweet via DB."""
    db = get_db()
    row = db.execute(
        "SELECT 1 FROM engagement_log WHERE niche_id = ? AND platform = 'x' AND action = 'respond' AND reply_to_tweet_id = ? LIMIT 1",
        (_niche_id, tweet_id),
    ).fetchone()
    return row is not None


def _build_response_prompt(niche_id: str) -> str:
    niche = get_niche(niche_id)
    voice_guide = load_voice_guide(niche_id)

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
            model=get_model("evaluator"),
            max_tokens=200,
            system=_build_eval_prompt(niche_id),
            messages=[{"role": "user", "content": prompt}],
        )
        text = response.content[0].text
        result = parse_json_response(text)
        if result:
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
            model=get_model("reply_drafter"),
            max_tokens=280,
            system=_build_response_prompt(niche_id),
            messages=[{"role": "user", "content": prompt}],
        )
        text = response.content[0].text.strip()
        if text.startswith('"') and text.endswith('"'):
            text = text[1:-1]
        # Humanizer check
        from tools.humanizer import validate_text
        hv = validate_text(text)
        if not hv.passed:
            log.warning(f"Humanizer rejected response to @{replier_handle}: {', '.join(hv.violations[:3])}")
            return ""
        return text
    except Exception as e:
        log.error(f"Failed to draft response to @{replier_handle}: {e}")
        return ""


def load_since_id() -> str | None:
    """Load the last processed mention ID from DB."""
    db = get_db()
    row = db.execute(
        "SELECT value FROM kv_store WHERE key = ?",
        (f"respond_since_id_{_niche_id}",),
    ).fetchone()
    return row["value"] if row else None


def save_since_id(since_id: str):
    db = get_db()
    db.execute(
        "INSERT OR REPLACE INTO kv_store (key, value) VALUES (?, ?)",
        (f"respond_since_id_{_niche_id}", since_id),
    )
    db.commit()


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

    global _niche_id
    _niche_id = niche_id

    log.info(f"Checking replies for @{handle} ({'DRY RUN' if dry_run else 'LIVE'})")

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
    candidates = []
    for m in mentions:
        # Skip if no parent tweet (not a reply)
        if not m.parent_tweet_id:
            continue

        # Skip if already responded
        if already_responded_to_tweet(m.tweet_id):
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

            log_engagement(
                niche_id, "x", "respond",
                reply_to_tweet_id=c["tweet_id"],
                our_response_id=reply_id,
                replier=c["replier"],
                reply_text=c["reply_text"],
                our_response=response_text,
                parent_id=c.get("parent_id"),
                score=c["eval"]["score"],
            )
        else:
            log.warning(f"Failed to post response to @{c['replier']}")

    # Save since_id so next run starts from where we left off
    if not dry_run:
        save_since_id(highest_id)

    log.info(f"Done. Responses: {responses_done}")


if __name__ == "__main__":
    # Parse niche early for niche-specific lock
    _pre = argparse.ArgumentParser(add_help=False)
    _pre.add_argument("--niche", default="tatamispaces")
    _pre_args, _ = _pre.parse_known_args()
    lock_name = f"respond_{_pre_args.niche}"

    if not acquire_process_lock(lock_name):
        print("Another respond instance is running. Skipping.")
        sys.exit(0)
    try:
        asyncio.run(main())
    finally:
        release_process_lock(lock_name)
