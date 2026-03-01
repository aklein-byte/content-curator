"""
Respond to replies and mentions on Bluesky.
Mirrors respond.py but uses AT Protocol notifications.

Usage: python bluesky_respond.py [--niche tatamispaces] [--dry-run]
"""

import sys
import os
import asyncio
import argparse
from pathlib import Path
from datetime import datetime, timezone, timedelta

sys.path.insert(0, str(Path(__file__).parent))

from dotenv import load_dotenv
load_dotenv()

from tools.bluesky import (
    get_notifications, get_post_thread, reply_to_post,
    set_niche as set_bsky_niche,
)
from tools.common import (
    load_json, save_json, random_delay, acquire_lock, release_lock,
    setup_logging, load_config, get_anthropic, load_voice_guide,
    get_model, parse_json_response,
)
from config.niches import get_niche

log = setup_logging("bluesky_respond")

BASE_DIR = Path(__file__).parent
MAX_RESPONSES_PER_RUN = 5
_delays = load_config().get("delays", {}).get("respond", [30, 90])
DELAY_MIN = _delays[0]
DELAY_MAX = _delays[1]

anthropic = get_anthropic()


def _cursor_path(niche_id: str) -> Path:
    return BASE_DIR / "data" / f"bluesky-respond-cursor-{niche_id}.txt"


def _response_log_path(niche_id: str) -> Path:
    return BASE_DIR / "data" / f"bluesky-response-log-{niche_id}.json"


def load_cursor(niche_id: str) -> str | None:
    p = _cursor_path(niche_id)
    if p.exists():
        return p.read_text().strip() or None
    return None


def save_cursor(niche_id: str, cursor: str):
    p = _cursor_path(niche_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(cursor)


def already_responded(log_entries: list, notif_uri: str) -> bool:
    return any(e.get("reply_to_uri") == notif_uri for e in log_entries)


def _build_eval_prompt(niche_id: str) -> str:
    niche = get_niche(niche_id)
    return f"""You evaluate replies to {niche['handle']}'s posts on Bluesky.

Decide if each reply deserves a response. Score 1-10:
- 9-10: Asked a specific question, added valuable info, or made a thoughtful comment.
- 7-8: Genuine engagement, worth a quick response.
- 5-6: Generic positive ("amazing!"). Maybe respond.
- 3-4: Low-effort (single emoji). Skip.
- 1-2: Spam, bot, unrelated. Skip.

Prioritize questions and replies that add information.

Return JSON:
{{
  "score": 8,
  "should_respond": true,
  "reason": "Brief explanation",
  "priority": "high"
}}

priority: "high" (questions, corrections), "medium" (genuine), "low" (generic)"""


def _build_response_prompt(niche_id: str) -> str:
    niche = get_niche(niche_id)
    voice_guide = load_voice_guide(niche_id)

    return f"""You write responses to people who reply to {niche['handle']}'s posts on Bluesky.

## Context
You are responding AS {niche['handle']}. Someone replied to one of your posts.

## Voice
{voice_guide if voice_guide else 'Knowledgeable, casual, specific. No AI slop.'}

## Rules
- 1-2 sentences max. Shorter is better.
- Match the energy of the reply.
- If someone asks a question you don't know, say so honestly.
- If the reply is just "wow" or "amazing" — "thanks!" is enough.
- No hashtags. No emojis unless the replier used them.
- Always respond in English.
- No em-dashes. No "not just X, it's Y".
- NEVER ask questions back.

Return ONLY the response text."""


async def evaluate_reply(our_text: str, reply_text: str, replier: str, niche_id: str) -> dict:
    """Evaluate if a reply deserves a response. Try CLI first, fall back to API."""
    # Try cheap CLI eval
    try:
        from tools.claude_runner import claude_json
        system = _build_eval_prompt(niche_id)
        prompt = f"Our post: {our_text}\n\nReply from @{replier}: {reply_text}\n\nShould we respond?"
        result = claude_json(system, prompt, timeout=20)
        if result and "score" in result:
            return {
                "score": result.get("score", 5),
                "should_respond": result.get("should_respond", False),
                "reason": result.get("reason", ""),
                "priority": result.get("priority", "medium"),
            }
    except Exception:
        pass

    # Fall back to API
    try:
        prompt = f"Our post: {our_text}\n\nReply from @{replier}: {reply_text}\n\nShould we respond?"
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
        log.error(f"Failed to evaluate reply from @{replier}: {e}")

    return {"score": 5, "should_respond": False, "reason": "Evaluation failed", "priority": "low"}


async def draft_response(our_text: str, reply_text: str, replier: str, niche_id: str) -> str:
    """Draft a response using Anthropic API (quality matters for replies)."""
    prompt = f"Your original post: {our_text}\n\n@{replier} replied: {reply_text}\n\nWrite your response:"
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
        from tools.humanizer import validate_text
        hv = validate_text(text)
        if not hv.passed:
            log.warning(f"Humanizer rejected response to @{replier}: {', '.join(hv.violations[:3])}")
            return ""
        return text
    except Exception as e:
        log.error(f"Failed to draft response to @{replier}: {e}")
        return ""


async def main():
    parser = argparse.ArgumentParser(description="Respond to Bluesky replies")
    parser.add_argument("--niche", default="tatamispaces")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--max-responses", type=int, default=MAX_RESPONSES_PER_RUN)
    args = parser.parse_args()

    niche_id = args.niche
    dry_run = args.dry_run
    max_responses = args.max_responses
    niche = get_niche(niche_id)
    set_bsky_niche(niche_id)

    bsky_env = niche.get("bluesky_env", {})
    our_handle = os.environ.get(bsky_env.get("handle", ""), "").lower()

    log.info(f"Checking Bluesky replies for {niche['handle']} ({'DRY RUN' if dry_run else 'LIVE'})")

    response_log = load_json(_response_log_path(niche_id))
    if not isinstance(response_log, list):
        response_log = []

    # Get notifications
    notifications = get_notifications(limit=50, reasons=["reply", "mention"])
    if not notifications:
        log.info("No new notifications")
        log.info("Done. Responses: 0")
        return

    log.info(f"Found {len(notifications)} reply/mention notifications")

    # Filter candidates
    candidates = []
    for n in notifications:
        if already_responded(response_log, n["uri"]):
            continue

        # Skip old (>48h)
        try:
            notif_time = datetime.fromisoformat(n["indexed_at"].replace("Z", "+00:00"))
            if datetime.now(timezone.utc) - notif_time > timedelta(hours=48):
                continue
        except Exception:
            pass

        # Get parent post text for context
        parent_text = "[our post]"
        parent_uri = n.get("parent_uri")
        parent_cid = n.get("parent_cid")
        root_uri = n.get("root_uri")
        root_cid = n.get("root_cid")

        if parent_uri:
            thread = get_post_thread(parent_uri, depth=0)
            if thread:
                parent_text = thread.get("text", parent_text)

        candidates.append({
            "uri": n["uri"],
            "cid": n["cid"],
            "reply_text": n["text"],
            "replier": n["author_handle"],
            "replier_did": n["author_did"],
            "parent_text": parent_text,
            "parent_uri": parent_uri or n["uri"],
            "parent_cid": parent_cid or n["cid"],
            "root_uri": root_uri or parent_uri or n["uri"],
            "root_cid": root_cid or parent_cid or n["cid"],
            "indexed_at": n["indexed_at"],
        })

    log.info(f"{len(candidates)} candidates to evaluate")

    if not candidates:
        log.info("Done. Responses: 0")
        return

    # Evaluate
    evaluated = []
    for c in candidates:
        ev = await evaluate_reply(c["parent_text"], c["reply_text"], c["replier"], niche_id)
        c["eval"] = ev
        log.info(f"  @{c['replier']} — score {ev['score']}/10 ({ev['reason'][:50]})")
        evaluated.append(c)

    # Sort by priority + score
    priority_order = {"high": 0, "medium": 1, "low": 2}
    evaluated.sort(key=lambda x: (priority_order.get(x["eval"]["priority"], 2), -x["eval"]["score"]))

    # Respond
    responses_done = 0
    for c in evaluated:
        if responses_done >= max_responses:
            break
        if not c["eval"]["should_respond"]:
            continue

        if dry_run:
            log.info(f"[DRY] Would respond to @{c['replier']}: \"{c['reply_text'][:60]}...\"")
            responses_done += 1
            continue

        response_text = await draft_response(c["parent_text"], c["reply_text"], c["replier"], niche_id)
        if not response_text:
            log.warning(f"Empty response draft for @{c['replier']}, skipping")
            continue

        log.info(f"Responding to @{c['replier']}: \"{response_text[:80]}\"")

        if responses_done > 0:
            await random_delay("responding", DELAY_MIN, DELAY_MAX)

        reply_uri = reply_to_post(
            parent_uri=c["uri"], parent_cid=c["cid"],
            root_uri=c["root_uri"], root_cid=c["root_cid"],
            text=response_text,
        )
        if reply_uri:
            responses_done += 1
            log.info(f"Responded to @{c['replier']} ({responses_done}/{max_responses})")
            response_log.append({
                "reply_to_uri": c["uri"],
                "our_response_uri": reply_uri,
                "replier": c["replier"],
                "reply_text": c["reply_text"],
                "our_response": response_text,
                "parent_uri": c["parent_uri"],
                "score": c["eval"]["score"],
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "action": "respond",
                "platform": "bluesky",
            })
            save_json(_response_log_path(niche_id), response_log)

    log.info(f"Done. Responses: {responses_done}")


if __name__ == "__main__":
    lock_fd = acquire_lock(BASE_DIR / ".bluesky_respond.lock")
    if not lock_fd:
        print("Another bluesky_respond is running. Skipping.")
        sys.exit(0)
    try:
        asyncio.run(main())
    finally:
        release_lock(lock_fd)
