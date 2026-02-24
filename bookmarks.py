"""
Bookmark-to-post pipeline for @tatamispaces.
Fetches your X bookmarks, evaluates them, drafts captions,
and adds them to posts.json as drafts for review.

Usage: python bookmarks.py [--niche tatamispaces] [--max-drafts 10]
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

from tools.xapi import get_bookmarks, get_tweet_by_id, XPost, set_niche as set_xapi_niche, check_image_urls_quality
from tools.common import load_json, save_json, notify, acquire_lock, release_lock, setup_logging, load_config, get_anthropic
from agents.engager import evaluate_post, draft_original_post
from agents.fact_checker import fact_check_draft, SourceContext
from config.niches import get_niche

log = setup_logging("bookmarks")

BASE_DIR = Path(__file__).parent
POSTS_FILE: Path = None  # resolved in main() from niche config


def validate_draft(caption: str, source_text: str, author: str, image_urls: list[str], enriched_context: str = "") -> tuple[bool, str]:
    """Use Sonnet to QA a draft before it enters the queue.

    Checks that the caption:
    - Has real body text (not just a credit line)
    - Makes sense as a standalone post
    - Appropriately references what the images show
    - Doesn't hallucinate details not in the source

    Returns (ok, reason).
    """
    client = get_anthropic()
    image_list = "\n".join(f"  {i+1}. {url}" for i, url in enumerate(image_urls))

    context_section = ""
    if enriched_context:
        context_section = f"\nEnriched context (vision/links/thread) provided to the drafter:\n{enriched_context}\n"

    try:
        response = client.messages.create(
            model=load_config().get("models", {}).get("evaluator", "claude-sonnet-4-20250514"),
            max_tokens=256,
            messages=[{"role": "user", "content": f"""You are a QA reviewer for a social media post.

Source post by @{author}:
{source_text}
{context_section}
Images ({len(image_urls)}):
{image_list}

Draft caption to publish:
{caption}

Answer these questions:
1. Does the caption have real body text, or is it just a credit line / attribution?
2. Does the caption make sense as a standalone post someone would want to read?
3. CRITICAL — Does the draft contain specific facts (architect names, dates, locations, materials, dimensions) that are NOT in the source text or enriched context above? If the draft names an architect, city, year, or material that doesn't appear in the source or context, it is hallucinated and must FAIL.
4. Does the caption add value beyond just restating the source?

Respond with EXACTLY one line:
PASS — if the draft is good to post
FAIL: <brief reason> — if the draft should be rejected"""}],
        )
        result = response.content[0].text.strip().split("\n")[0]
        if result.startswith("PASS"):
            return True, "ok"
        else:
            reason = result.replace("FAIL:", "").replace("FAIL", "").strip() or "QA rejected"
            return False, reason
    except Exception as e:
        log.warning(f"Draft QA call failed: {e} — allowing draft through")
        return True, "qa-error-passthrough"

async def enrich_context(post: XPost) -> str:
    """Gather rich context for a bookmarked post before drafting a caption.

    Assembles vision descriptions, thread context, and link content
    into a context string passed to draft_original_post().
    """
    import re
    import base64
    import httpx

    parts = []

    # --- Step A: Vision — describe what the images show ---
    if post.image_urls:
        try:
            content_blocks = []
            downloaded = 0
            for url in post.image_urls[:4]:
                try:
                    resp = httpx.get(url, timeout=10, follow_redirects=True)
                    if resp.status_code != 200:
                        continue
                    ct = resp.headers.get("content-type", "")
                    if "image" not in ct:
                        continue
                    if len(resp.content) < 5000:
                        continue
                    media_type = ct.split(";")[0].strip()
                    if media_type not in ("image/jpeg", "image/png", "image/webp", "image/gif"):
                        media_type = "image/jpeg"
                    img_bytes = resp.content
                    if len(img_bytes) > 4_500_000:
                        from PIL import Image as _PILImg
                        import io as _io
                        pil = _PILImg.open(_io.BytesIO(img_bytes))
                        pil.thumbnail((1920, 1920))
                        buf = _io.BytesIO()
                        pil.save(buf, format="JPEG", quality=85)
                        img_bytes = buf.getvalue()
                        media_type = "image/jpeg"
                    b64 = base64.b64encode(img_bytes).decode()
                    content_blocks.append({
                        "type": "image",
                        "source": {"type": "base64", "media_type": media_type, "data": b64},
                    })
                    downloaded += 1
                except Exception as e:
                    log.debug(f"  Vision: failed to download {url[:60]}: {e}")

            if content_blocks:
                content_blocks.append({
                    "type": "text",
                    "text": "Describe what these images show. Be specific about architecture, materials, spaces, objects, and any visible text or signage.",
                })
                client = get_anthropic()
                _vision_model = load_config().get("models", {}).get("enrich_vision", "claude-sonnet-4-20250514")
                vision_resp = client.messages.create(
                    model=_vision_model,
                    max_tokens=300,
                    messages=[{"role": "user", "content": content_blocks}],
                )
                desc = vision_resp.content[0].text.strip()
                parts.append(f"Images show: {desc}")
                log.info(f"  Context enrichment — vision ({downloaded} images): {desc[:80]}...")
        except Exception as e:
            log.warning(f"  Context enrichment — vision failed: {e}")

    # --- Step B: Thread context — fetch root tweet if this is a reply ---
    if post.conversation_id and post.conversation_id != post.post_id:
        try:
            root = get_tweet_by_id(post.conversation_id)
            if root and root.text:
                parts.append(f"Thread root by @{root.author_handle}: {root.text}")
                log.info(f"  Context enrichment — thread root: @{root.author_handle}: {root.text[:60]}...")
        except Exception as e:
            log.warning(f"  Context enrichment — thread fetch failed: {e}")

    # --- Step C: Link content — extract and fetch URLs ---
    urls = re.findall(r'https?://\S+', post.text)
    # Filter out Twitter/X internal links
    urls = [u for u in urls if not any(d in u for d in ['t.co', 'twitter.com', 'pic.twitter.com', 'x.com'])]
    for url in urls[:2]:
        try:
            import requests as _requests
            resp = _requests.get(url, timeout=10, headers={
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
            }, allow_redirects=True)
            if resp.status_code == 200:
                text = resp.text[:8000]
                # Strip HTML tags with simple regex
                clean = re.sub(r'<[^>]+>', ' ', text)
                clean = re.sub(r'\s+', ' ', clean).strip()[:2000]
                if len(clean) > 100:
                    parts.append(f"Link content ({url[:60]}): {clean}")
                    log.info(f"  Context enrichment — link: {url[:60]} ({len(clean)} chars)")
        except Exception as e:
            log.debug(f"  Context enrichment — link fetch failed for {url[:60]}: {e}")

    context = "\n\n".join(parts)
    if not context:
        log.info("  Context enrichment — no additional context gathered")
    return context


_pw = load_config().get("posting_window", {})


def load_posts() -> dict:
    return load_json(POSTS_FILE, default={"posts": []})


def save_posts(data: dict):
    save_json(POSTS_FILE, data)


def next_post_id(posts_data: dict) -> int:
    existing_ids = [p.get("id", 0) for p in posts_data.get("posts", [])]
    return max(existing_ids, default=0) + 1


MAX_PER_DAY = _pw.get("max_per_day", 4)
MIN_GAP_HOURS = _pw.get("min_gap_hours", 2)
WINDOW_START = _pw.get("start_hour_et", 7)
WINDOW_END = _pw.get("end_hour_et", 22)


def next_schedule_slot(posts_data: dict) -> datetime:
    """Find the next available posting slot with random timing across the day."""
    import random
    from zoneinfo import ZoneInfo
    et = ZoneInfo("America/New_York")

    # Collect all scheduled times as datetimes
    taken_times = []
    for p in posts_data.get("posts", []):
        sf = p.get("scheduled_for")
        if sf and p.get("status") in ("approved", "posted", "scheduled_native"):
            try:
                taken_times.append(datetime.fromisoformat(sf).astimezone(et))
            except Exception:
                pass

    now_et = datetime.now(et)
    check_date = now_et.date()
    if now_et.hour >= WINDOW_END:
        check_date += timedelta(days=1)

    for day_offset in range(30):
        d = check_date + timedelta(days=day_offset)

        posts_on_day = sum(1 for t in taken_times if t.date() == d)
        if posts_on_day >= MAX_PER_DAY:
            continue

        # Try random times within the window, up to 20 attempts
        for _ in range(20):
            hour = random.randint(WINDOW_START, WINDOW_END - 1)
            minute = random.randint(0, 59)
            candidate = datetime(d.year, d.month, d.day, hour, minute, tzinfo=et)

            if candidate <= now_et:
                continue

            too_close = any(
                abs((candidate - t).total_seconds()) < MIN_GAP_HOURS * 3600
                for t in taken_times
            )
            if too_close:
                continue

            return candidate

    return datetime.now(et) + timedelta(days=30)


MAX_REPOST_QUEUE = 10  # Skip run if this many reposts are already waiting


def already_in_queue(posts_data: dict, post_id: str) -> bool:
    """Check if a post ID or source URL containing it is already queued."""
    for p in posts_data.get("posts", []):
        src = p.get("source_url") or ""
        if post_id in src:
            return True
    return False


async def main():
    parser = argparse.ArgumentParser(description="Turn bookmarks into post drafts")
    parser.add_argument("--niche", default="tatamispaces", help="Niche ID")
    parser.add_argument("--max-drafts", type=int, default=10, help="Max new drafts to create")
    parser.add_argument("--min-score", type=int, default=7, help="Minimum relevance score (1-10)")
    args = parser.parse_args()

    niche_id = args.niche
    niche = get_niche(niche_id)

    # Resolve niche-aware posts file
    global POSTS_FILE
    POSTS_FILE = Path(os.environ.get("POSTS_FILE", str(BASE_DIR / niche.get("posts_file", "posts.json"))))

    log.info(f"Fetching bookmarks for {niche['handle']}")

    # Set API credentials for this niche
    set_xapi_niche(niche_id)

    # Fetch bookmarks via official API v2 (OAuth 2.0)
    bookmark_posts = get_bookmarks(max_results=40)
    log.info(f"Got {len(bookmark_posts)} bookmarks")

    # Load existing posts to check queue depth and skip duplicates
    posts_data = load_posts()

    # Skip if enough reposts already queued (same pattern as quote_drafts.py)
    queued = sum(
        1 for p in posts_data.get("posts", [])
        if p.get("type") == "repost-with-credit" and p.get("status") == "approved"
    )
    if queued >= MAX_REPOST_QUEUE:
        log.info(f"Already {queued} reposts in queue (approved). Skipping bookmark run.")
        print()
        print("=" * 60)
        print(f"BOOKMARKS SKIPPED for {niche['handle']}")
        print("=" * 60)
        print(f"Already {queued} approved reposts in queue (max {MAX_REPOST_QUEUE})")
        print()
        return

    # Cap max drafts to remaining queue space
    remaining_slots = MAX_REPOST_QUEUE - queued
    effective_max = min(args.max_drafts, remaining_slots)
    log.info(f"Queue: {queued} approved reposts, {remaining_slots} slots remaining (cap: {effective_max})")

    # Filter: must have images
    with_images = [p for p in bookmark_posts if len(p.image_urls) > 0]
    log.info(f"{len(with_images)} have images")

    # Evaluate and draft
    drafts_created = 0
    skipped = 0

    for post in with_images:
        if drafts_created >= effective_max:
            break

        if already_in_queue(posts_data, post.post_id):
            skipped += 1
            continue

        # Evaluate relevance
        evaluation = await evaluate_post(
            post_text=post.text,
            author=post.author_handle,
            niche_id=niche_id,
            image_count=len(post.image_urls),
            likes=post.likes,
            reposts=post.reposts,
        )

        try:
            score = int(evaluation.get("relevance_score") or 0)
        except (TypeError, ValueError):
            score = 0
        reason = evaluation.get("reason", "no reason") or "no reason"
        if score < args.min_score:
            log.info(f"  Skip @{post.author_handle} — score {score}/10: {reason[:50]}")
            continue

        log.info(f"  @{post.author_handle} — score {score}/10, {post.likes} likes")

        # Pre-check image quality before spending API calls on caption drafting
        has_good_images, img_details = check_image_urls_quality(post.image_urls)
        if not has_good_images:
            log.info(f"  Skip @{post.author_handle} — all images too small:")
            for d in img_details:
                log.info(d)
            continue

        # Enrich context before drafting (vision, thread, links)
        context = await enrich_context(post)

        # Draft caption
        caption_data = await draft_original_post(
            source_text=post.text,
            author=post.author_handle,
            niche_id=niche_id,
            image_description=context if context else None,
        )

        caption_text = caption_data.get("text", "")
        if not caption_text:
            log.warning(f"  Empty caption for @{post.author_handle}, skipping")
            continue

        # Fact-check caption against source tweet
        fc_source = SourceContext.from_bookmark(post.text, post.author_handle)
        fc_story, _fc_verifications = fact_check_draft(
            {"tweets": [{"text": caption_text}]}, fc_source,
        )
        if fc_story is None:
            log.warning(f"  Fact-check rejected draft for @{post.author_handle}")
            continue
        caption_text = fc_story["tweets"][0]["text"]

        # QA check: Sonnet validates the draft makes sense and references images
        qa_ok, qa_reason = validate_draft(caption_text, post.text, post.author_handle, post.image_urls, enriched_context=context)
        if not qa_ok:
            log.warning(f"  Draft QA failed for @{post.author_handle}: {qa_reason}")
            continue

        if len(caption_text) > 4000:
            log.warning(f"  Caption too long ({len(caption_text)} chars) for @{post.author_handle}, skipping")
            continue

        source_url = f"https://x.com/{post.author_handle}/status/{post.post_id}"
        sched = next_schedule_slot(posts_data)

        new_post = {
            "id": next_post_id(posts_data),
            "type": "repost-with-credit",
            "text": caption_text,
            "image": None,
            "image_urls": post.image_urls,
            "source_url": source_url,
            "source_handle": f"@{post.author_handle}",
            "status": "approved",
            "score": score,
            "scheduled_for": sched.isoformat(),
            "notes": f"From bookmarks. {post.likes} likes. {evaluation['reason'][:80]}",
        }

        posts_data["posts"].append(new_post)
        drafts_created += 1
        log.info(f"  Scheduled #{new_post['id']} for {sched.strftime('%b %d %I%p ET')}: {caption_text[:60]}...")

    save_posts(posts_data)

    # Summary
    print()
    print("=" * 60)
    print(f"BOOKMARKS PROCESSED for {niche['handle']}")
    print("=" * 60)
    print(f"Bookmarks fetched:  {len(bookmark_posts)}")
    print(f"With images:        {len(with_images)}")
    print(f"Already in queue:   {skipped}")
    print(f"New drafts created: {drafts_created}")
    print()

    if drafts_created > 0:
        print("New drafts:")
        for p in posts_data["posts"]:
            if p.get("status") == "draft":
                print(f"  #{p['id']} — {p['text'][:70]}...")
                print(f"          from {p.get('source_handle', '?')}")
        print()
        print(f"Review in {POSTS_FILE}")
        print("Change status to 'approved' and add 'scheduled_for' to publish.")

    if drafts_created > 0:
        notify(f"{niche['handle']} bookmarks", f"{drafts_created} new drafts from bookmarks")


if __name__ == "__main__":
    lock_fd = acquire_lock(BASE_DIR / ".bookmarks.lock")
    if not lock_fd:
        log.info("Another bookmarks.py is already running, exiting")
        sys.exit(0)
    try:
        asyncio.run(main())
    finally:
        release_lock(lock_fd)
