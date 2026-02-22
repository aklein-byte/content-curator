"""
Instagram engagement script — niche-aware.
Searches hashtags, auto-likes high-relevance content,
drafts comments for review, follows relevant accounts.

Uses direct HTTP requests with session cookies (no browser needed).

Usage: python ig_engage.py [--niche tatamispaces] [--dry-run]
"""

import sys
import os
import json
import asyncio
import random
import argparse
from pathlib import Path
from datetime import datetime, timezone
from dataclasses import dataclass

sys.path.insert(0, str(Path(__file__).parent))

from dotenv import load_dotenv
load_dotenv()

from config.niches import get_niche
from tools.common import load_json, save_json, notify, random_delay, acquire_lock, release_lock, setup_logging, load_config, niche_log_path, get_anthropic

log = setup_logging("ig_engage")

BASE_DIR = Path(__file__).parent
IG_ENGAGEMENT_LOG: Path = None  # resolved in main() from niche config

_cfg = load_config()

# Limits per run
MAX_LIKES = 15
MAX_COMMENTS = 5
MAX_FOLLOWS = 7
HASHTAGS_PER_RUN = _cfg.get("ig_hashtags_per_run", 5)

# Delay range from config (seconds)
_delays = _cfg.get("delays", {}).get("ig_engage", [15, 45])
DELAY_MIN = _delays[0]
DELAY_MAX = _delays[1]

# Hashtags from config (fallback)
IG_HASHTAGS = _cfg.get("ig_hashtags", [
    "japaneseinterior",
    "japanesehouse",
    "japaneseaesthetics",
    "tatami",
    "japandi",
    "tokonoma",
    "engawa",
    "mingei",
    "japanesearchitecture",
    "japanesedesign",
    "washitsu",
    "kominka",
    "ryokan",
    "machiya",
    "wabishabi",
    "shoji",
    "japanesegarden",
    "minimalistjapan",
])


@dataclass
class IGPost:
    shortcode: str
    media_id: str
    author: str
    user_id: str
    caption: str
    likes: int
    url: str


def load_log() -> list:
    return load_json(IG_ENGAGEMENT_LOG, default=[])


def save_log(data: list):
    save_json(IG_ENGAGEMENT_LOG, data)


def already_engaged(log_entries: list, shortcode: str, action: str) -> bool:
    return any(
        e.get("shortcode") == shortcode and e.get("action") == action
        for e in log_entries
    )


async def evaluate_ig_post(post: IGPost, niche_id: str) -> dict:
    """Evaluate an IG post using Claude."""
    client = get_anthropic()
    niche = get_niche(niche_id)

    prompt = f"""Evaluate this Instagram post for the account {niche['handle']} ({niche['description']}).

Author: @{post.author}
Caption: {post.caption if post.caption else '[no caption / image only]'}
Likes: {post.likes}

Score 1-10 on relevance to our niche.
Consider: Does the caption mention topics relevant to {niche['handle']}? Is the author in our niche?

Return JSON:
{{"relevance_score": 8, "should_engage": true, "reason": "Brief explanation", "suggested_actions": ["like", "comment"]}}

Possible actions: like, comment, follow
- "like" if score >= 6
- "comment" if score >= 7 and we have something useful to say
- "follow" if the author consistently posts great niche content (score 9-10)"""

    try:
        response = client.messages.create(
            model=_cfg.get("models", {}).get("evaluator", "claude-haiku-4-5-20251001"),
            max_tokens=256,
            messages=[{"role": "user", "content": prompt}],
        )
        text = response.content[0].text
        json_start = text.find("{")
        json_end = text.rfind("}") + 1
        if json_start >= 0 and json_end > json_start:
            return json.loads(text[json_start:json_end])
    except Exception as e:
        log.error(f"  Eval failed for @{post.author}: {e}")

    return {"relevance_score": 5, "should_engage": False, "reason": "eval failed", "suggested_actions": []}


async def draft_ig_comment(post: IGPost, niche_id: str) -> str:
    """Draft a comment using Claude."""
    client = get_anthropic()

    prompt = f"""Write a short Instagram comment on this post.

Author: @{post.author}
Caption: {post.caption if post.caption else '[no caption / image only]'}

Rules:
- 1 sentence max. Keep it short.
- You CANNOT see the image. Only respond to the caption text.
- If caption is empty or just emojis, ask a simple question like "Where is this?" or say something brief like "Beautiful space"
- No hashtags in comments.
- Sound like a real person, not a bot. No "amazing!", "stunning!", "love this!" generic filler.
- Ask a real question or add a specific observation based on what the caption says.
- English only.

Return ONLY the comment text. Nothing else."""

    try:
        response = client.messages.create(
            model=_cfg.get("models", {}).get("reply_drafter", "claude-opus-4-6"),
            max_tokens=150,
            messages=[{"role": "user", "content": prompt}],
        )
        comment = response.content[0].text.strip()
        if comment.startswith('"') and comment.endswith('"'):
            comment = comment[1:-1]
        return comment
    except Exception as e:
        log.error(f"  Comment draft failed for @{post.author}: {e}")
        return ""


async def main():
    parser = argparse.ArgumentParser(description="Engage on Instagram")
    parser.add_argument("--niche", default="tatamispaces", help="Niche ID")
    parser.add_argument("--dry-run", action="store_true", help="Evaluate only, no actions")
    parser.add_argument("--max-likes", type=int, default=MAX_LIKES)
    parser.add_argument("--max-comments", type=int, default=MAX_COMMENTS)
    parser.add_argument("--max-follows", type=int, default=MAX_FOLLOWS)
    args = parser.parse_args()

    niche_id = args.niche
    dry_run = args.dry_run
    niche = get_niche(niche_id)

    # Resolve niche-aware engagement log
    global IG_ENGAGEMENT_LOG
    IG_ENGAGEMENT_LOG = Path(os.environ.get(
        "IG_ENGAGEMENT_LOG", str(niche_log_path("ig-engagement-log.json", niche_id))
    ))

    log.info(f"IG engagement for {niche['handle']} ({'DRY RUN' if dry_run else 'LIVE'})")

    # Initialize web client with cookies
    from tools.ig_web_client import IGWebClient
    try:
        client = IGWebClient(niche_id=niche_id)
    except (FileNotFoundError, ValueError) as e:
        log.error(f"Cannot start IG engage: {e}")
        notify(f"{niche['handle']} IG", f"IG engage failed: {e}")
        return

    # Verify session
    if not client.check_session():
        log.error("IG session invalid — need fresh cookies")
        notify(f"{niche['handle']} IG", "IG engage: session expired, need fresh cookies")
        return

    # Load engagement log
    eng_log = load_log()

    # Pick random hashtags for this run — niche-aware
    niche_hashtags = niche.get("ig_hashtags", IG_HASHTAGS)
    hashtags = random.sample(niche_hashtags, min(HASHTAGS_PER_RUN, len(niche_hashtags)))
    log.info(f"Hashtags this run: {', '.join('#' + h for h in hashtags)}")

    # Fetch posts from hashtag feeds
    all_posts: list[IGPost] = []
    seen_shortcodes = set()

    for hashtag in hashtags:
        raw_posts = client.get_hashtag_feed(hashtag, max_posts=9)
        for p in raw_posts:
            sc = p["shortcode"]
            if sc not in seen_shortcodes:
                seen_shortcodes.add(sc)
                all_posts.append(IGPost(
                    shortcode=sc,
                    media_id=p["media_id"],
                    author=p["author"],
                    user_id=p["user_id"],
                    caption=p["caption"],
                    likes=p["likes"],
                    url=p["url"],
                ))
        await random_delay("next hashtag", DELAY_MIN, DELAY_MAX)

    log.info(f"Total unique posts found: {len(all_posts)}")

    if not all_posts:
        log.warning("No posts found — hashtag feeds may be empty or session blocked")
        return

    # Evaluate posts with Claude
    scored = []
    for post in all_posts:
        if already_engaged(eng_log, post.shortcode, "like") and already_engaged(eng_log, post.shortcode, "comment"):
            continue
        evaluation = await evaluate_ig_post(post, niche_id)
        scored.append((post, evaluation))
        log.info(
            f"  @{post.author} — score {evaluation.get('relevance_score', 0)}/10 "
            f"({evaluation.get('reason', '')[:50]})"
        )

    scored.sort(key=lambda x: x[1].get("relevance_score", 0), reverse=True)

    # --- Likes ---
    likes_done = 0
    for post, ev in scored:
        if likes_done >= args.max_likes:
            break
        if ev.get("relevance_score", 0) < 6:
            continue
        if already_engaged(eng_log, post.shortcode, "like"):
            continue

        if dry_run:
            log.info(f"  [DRY] Would like @{post.author} (score {ev['relevance_score']})")
            likes_done += 1
        else:
            await random_delay("like", DELAY_MIN, DELAY_MAX)
            if client.like_post(post.media_id):
                likes_done += 1
                eng_log.append({
                    "action": "like",
                    "shortcode": post.shortcode,
                    "author": post.author,
                    "score": ev["relevance_score"],
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                })
                log.info(f"  Liked @{post.author} ({likes_done}/{args.max_likes})")

    # --- Comments ---
    comments_done = 0
    commented_authors = set()
    for post, ev in scored:
        if comments_done >= args.max_comments:
            break
        if ev.get("relevance_score", 0) < 7:
            continue
        if "comment" not in ev.get("suggested_actions", []):
            continue
        if already_engaged(eng_log, post.shortcode, "comment"):
            continue
        if post.author in commented_authors:
            continue

        comment = await draft_ig_comment(post, niche_id)
        if not comment:
            continue

        if dry_run:
            log.info(f"  [DRY] Would comment on @{post.author}: {comment[:60]}...")
            comments_done += 1
            commented_authors.add(post.author)
        else:
            await random_delay("comment", DELAY_MIN, DELAY_MAX)
            if client.comment_post(post.media_id, comment):
                comments_done += 1
                commented_authors.add(post.author)
                eng_log.append({
                    "action": "comment",
                    "shortcode": post.shortcode,
                    "author": post.author,
                    "comment": comment,
                    "score": ev["relevance_score"],
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                })
                log.info(f"  Commented on @{post.author} ({comments_done}/{args.max_comments}): {comment[:60]}...")

    # --- Follows ---
    follows_done = 0
    followed = {e["author"] for e in eng_log if e.get("action") == "follow"}
    for post, ev in scored:
        if follows_done >= args.max_follows:
            break
        if ev.get("relevance_score", 0) < 7:
            continue
        if post.author in followed:
            continue

        if dry_run:
            log.info(f"  [DRY] Would follow @{post.author}")
            follows_done += 1
            followed.add(post.author)
        else:
            await random_delay("follow", DELAY_MIN, DELAY_MAX)
            if client.follow_user(post.user_id):
                follows_done += 1
                followed.add(post.author)
                eng_log.append({
                    "action": "follow",
                    "shortcode": post.shortcode,
                    "author": post.author,
                    "score": ev["relevance_score"],
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                })
                log.info(f"  Followed @{post.author} ({follows_done}/{args.max_follows})")

    save_log(eng_log)

    summary = f"IG engage: {likes_done} likes, {comments_done} comments, {follows_done} follows"
    log.info(summary)
    if likes_done + comments_done + follows_done > 0:
        notify(f"{niche['handle']} IG", summary)


if __name__ == "__main__":
    # Parse niche early for niche-specific lock
    _pre = argparse.ArgumentParser(add_help=False)
    _pre.add_argument("--niche", default="tatamispaces")
    _pre_args, _ = _pre.parse_known_args()
    lock_file = BASE_DIR / f".ig_engage_{_pre_args.niche}.lock"

    lock_fd = acquire_lock(lock_file)
    if not lock_fd:
        log.info("Another ig_engage.py is already running for this niche, exiting")
        sys.exit(0)
    try:
        asyncio.run(main())
    finally:
        release_lock(lock_fd)
