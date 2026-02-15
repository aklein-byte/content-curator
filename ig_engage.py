"""
Instagram engagement script for @tatamispaces.
Searches hashtags, auto-likes high-relevance content,
drafts comments for review, follows relevant accounts.

Uses Playwright with Chrome cookies (same approach as ig_post.py).

Usage: python ig_engage.py [--niche tatamispaces] [--dry-run]
"""

import sys
import os
import json
import re
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
from tools.common import load_json, save_json, notify, random_delay, acquire_lock, release_lock, setup_logging, load_config

log = setup_logging("ig_engage")

BASE_DIR = Path(__file__).parent
IG_ENGAGEMENT_LOG = Path(os.environ.get(
    "IG_ENGAGEMENT_LOG", str(BASE_DIR / "ig-engagement-log.json")
))

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

# Hashtags from config
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
    author: str
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




async def scrape_hashtag_posts(page, hashtag: str, max_posts: int = 9) -> list[IGPost]:
    """Navigate to a hashtag page, collect shortcodes, then visit each post page."""
    url = f"https://www.instagram.com/explore/tags/{hashtag}/"
    log.info(f"Browsing #{hashtag}...")
    await page.goto(url, wait_until="domcontentloaded", timeout=30000)
    await page.wait_for_timeout(4000)

    # Dismiss any dialogs that might be blocking
    try:
        not_now = page.locator('button:has-text("Not now"), div[role="button"]:has-text("Not now")').first
        if await not_now.count() > 0:
            await not_now.click()
            await page.wait_for_timeout(1000)
    except Exception:
        pass

    # Collect shortcodes from the grid
    shortcodes = []
    links = await page.locator('a[href*="/p/"]').all()
    for link in links[:max_posts]:
        try:
            href = await link.get_attribute("href")
            if href and "/p/" in href:
                sc = href.split("/p/")[1].strip("/")
                if sc and sc not in shortcodes:
                    shortcodes.append(sc)
        except Exception:
            pass

    log.info(f"  Found {len(shortcodes)} post links on #{hashtag}")

    # Visit each post page directly (more reliable than modal extraction)
    posts = []
    for sc in shortcodes:
        try:
            post_url = f"https://www.instagram.com/p/{sc}/"
            await page.goto(post_url, wait_until="domcontentloaded", timeout=20000)
            await page.wait_for_timeout(2000)

            author = ""
            caption = ""
            likes = 0

            # Author — look for profile link in header
            for sel in ['header a[href*="/"]', 'article header a', 'a[role="link"][href*="/"]']:
                try:
                    el = page.locator(sel).first
                    if await el.count() > 0:
                        href = await el.get_attribute("href")
                        if href and href.count("/") <= 3:
                            author = href.strip("/").split("/")[-1]
                            if author and author not in ("p", "explore", "reels"):
                                break
                            author = ""
                except Exception:
                    pass

            # Caption — og:description is the most reliable source
            try:
                og = await page.locator('meta[property="og:description"]').get_attribute("content")
                if og:
                    # Strip the "X likes, Y comments - username on DATE: " prefix
                    m = re.search(r'on \w+ \d+, \d{4}.{0,5}[:"]\s*(.+)', og, re.DOTALL)
                    caption = (m.group(1) if m else og).strip().strip('"').strip()[:500]
            except Exception:
                pass
            if not caption:
                # Fallback: second h1 (first is always "Post")
                try:
                    h1s = await page.locator('h1').all()
                    for h in h1s:
                        text = (await h.inner_text()).strip()
                        if text and text != "Post" and len(text) > 3:
                            caption = text[:500]
                            break
                except Exception:
                    pass

            # Like count
            try:
                like_el = page.locator('section span, button span').filter(has_text="like")
                if await like_el.count() > 0:
                    like_text = await like_el.first.inner_text()
                    num = like_text.replace(",", "").split()[0]
                    likes = int(num)
            except Exception:
                pass

            if author:
                posts.append(IGPost(
                    shortcode=sc,
                    author=author,
                    caption=caption,
                    likes=likes,
                    url=post_url,
                ))
            else:
                log.debug(f"  Skipping {sc}: no author found")

        except Exception as e:
            log.warning(f"  Failed to scrape post {sc}: {e}")

    log.info(f"  Scraped {len(posts)} posts from #{hashtag}")
    return posts


async def evaluate_ig_post(post: IGPost, niche_id: str) -> dict:
    """Evaluate an IG post using Claude."""
    from anthropic import Anthropic
    client = Anthropic()
    niche = get_niche(niche_id)

    prompt = f"""Evaluate this Instagram post for the account {niche['handle']} ({niche['description']}).

Author: @{post.author}
Caption: {post.caption if post.caption else '[no caption / image only]'}
Likes: {post.likes}

Score 1-10 on relevance to Japanese interior design and architecture.
Consider: Does the caption mention Japanese design topics? Is the author in our niche?

Return JSON:
{{"relevance_score": 8, "should_engage": true, "reason": "Brief explanation", "suggested_actions": ["like", "comment"]}}

Possible actions: like, comment, follow
- "like" if score >= 6
- "comment" if score >= 7 and we have something useful to say
- "follow" if the author consistently posts great niche content (score 9-10)"""

    try:
        response = client.messages.create(
            model=_cfg.get("models", {}).get("evaluator", "claude-sonnet-4-20250514"),
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
    from anthropic import Anthropic
    client = Anthropic()

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


async def like_ig_post(page, post: IGPost) -> bool:
    """Like a post by navigating to it and clicking the heart."""
    try:
        await page.goto(post.url, wait_until="domcontentloaded", timeout=20000)
        await page.wait_for_timeout(2000)

        # Find the like button (heart SVG)
        like_btn = page.locator('svg[aria-label="Like"]').first
        if await like_btn.count() > 0:
            await like_btn.click()
            await page.wait_for_timeout(1000)
            return True
        else:
            # Already liked (would show "Unlike")
            log.info(f"  Already liked @{post.author}")
            return False
    except Exception as e:
        log.error(f"  Like failed @{post.author}: {e}")
        return False


async def comment_ig_post(page, post: IGPost, comment: str) -> bool:
    """Comment on a post."""
    try:
        await page.goto(post.url, wait_until="domcontentloaded", timeout=20000)
        await page.wait_for_timeout(2000)

        # Click comment icon or the comment textarea
        comment_area = page.locator('textarea[aria-label="Add a comment…"]').first
        if await comment_area.count() == 0:
            # Try clicking the comment icon to reveal the text area
            comment_icon = page.locator('svg[aria-label="Comment"]').first
            if await comment_icon.count() > 0:
                await comment_icon.click()
                await page.wait_for_timeout(1000)
            comment_area = page.locator('textarea[aria-label="Add a comment…"]').first

        if await comment_area.count() > 0:
            await comment_area.click()
            await page.wait_for_timeout(500)
            await comment_area.fill(comment)
            await page.wait_for_timeout(500)

            # Click Post button
            post_btn = page.locator('div[role="button"]:has-text("Post")').first
            if await post_btn.count() > 0:
                await post_btn.click()
                await page.wait_for_timeout(2000)
                return True

        log.warning(f"  Could not find comment area for @{post.author}")
        return False
    except Exception as e:
        log.error(f"  Comment failed @{post.author}: {e}")
        return False


async def follow_ig_user(page, post: IGPost) -> bool:
    """Follow a user from their profile page."""
    try:
        profile_url = f"https://www.instagram.com/{post.author}/"
        await page.goto(profile_url, wait_until="domcontentloaded", timeout=20000)
        await page.wait_for_timeout(3000)

        # Check if we landed on a login wall
        page_title = await page.title()
        if "Login" in page_title or "log in" in page_title.lower():
            log.error(f"  Login wall on profile page for @{post.author}")
            return False

        # Dismiss any "Turn on Notifications" or cookie popups
        try:
            not_now = page.locator('button:text-is("Not Now")').first
            if await not_now.count() > 0:
                await not_now.click()
                await page.wait_for_timeout(1000)
        except Exception:
            pass

        # Debug: list all button texts on page
        all_buttons = page.locator('button')
        btn_count = await all_buttons.count()
        btn_texts = []
        for i in range(min(btn_count, 20)):
            try:
                txt = (await all_buttons.nth(i).inner_text()).strip()
                if txt:
                    btn_texts.append(txt)
            except Exception:
                pass
        log.info(f"  @{post.author} page buttons: {btn_texts[:10]}")

        # Try exact text match first
        follow_btn = page.locator('button:text-is("Follow")').first
        if await follow_btn.count() == 0:
            # Fallback: look for button containing just "Follow" (not "Following", "Follow Back")
            all_follow = page.locator('button:has-text("Follow")')
            for i in range(await all_follow.count()):
                txt = (await all_follow.nth(i).inner_text()).strip()
                if txt == "Follow":
                    follow_btn = all_follow.nth(i)
                    break

        if await follow_btn.count() > 0:
            btn_text = (await follow_btn.inner_text()).strip()
            if btn_text == "Follow":
                await follow_btn.click()
                await page.wait_for_timeout(1500)
                log.info(f"  Followed @{post.author}")
                return True
            else:
                log.info(f"  Already following @{post.author} (button says '{btn_text}')")
                return False
        else:
            log.warning(f"  No Follow button found on @{post.author} profile page (title: {page_title})")
            return False
    except Exception as e:
        log.error(f"  Follow failed @{post.author}: {e}")
        return False




async def main():
    parser = argparse.ArgumentParser(description="Engage on Instagram")
    parser.add_argument("--niche", default="tatamispaces", help="Niche ID")
    parser.add_argument("--dry-run", action="store_true", help="Evaluate only, no actions")
    parser.add_argument("--max-likes", type=int, default=MAX_LIKES)
    parser.add_argument("--max-comments", type=int, default=MAX_COMMENTS)
    parser.add_argument("--max-follows", type=int, default=MAX_FOLLOWS)
    parser.add_argument("--headless", action="store_true", default=True, help="Run browser headless (default)")
    parser.add_argument("--no-headless", dest="headless", action="store_false", help="Show browser window")
    args = parser.parse_args()

    niche_id = args.niche
    dry_run = args.dry_run
    niche = get_niche(niche_id)

    log.info(f"IG engagement for {niche['handle']} ({'DRY RUN' if dry_run else 'LIVE'})")

    # Load engagement log
    eng_log = load_log()

    # Pick random hashtags for this run
    hashtags = random.sample(IG_HASHTAGS, min(HASHTAGS_PER_RUN, len(IG_HASHTAGS)))
    log.info(f"Hashtags this run: {', '.join('#' + h for h in hashtags)}")

    # Launch Playwright browser with persistent profile
    from playwright.async_api import async_playwright
    from tools.ig_browser import get_ig_browser

    all_posts: list[IGPost] = []
    seen_shortcodes = set()

    async with async_playwright() as pw:
        context = await get_ig_browser(pw, headless=args.headless)
        page = context.pages[0] if context.pages else await context.new_page()

        # Go to IG home to confirm login
        await page.goto("https://www.instagram.com/", wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(4000)

        # Dismiss "Save your login info?" dialog if present
        try:
            not_now = page.locator('button:has-text("Not now"), div[role="button"]:has-text("Not now")').first
            if await not_now.count() > 0:
                await not_now.click()
                await page.wait_for_timeout(2000)
                log.info("Dismissed 'Save login info' dialog")
        except Exception:
            pass

        # Scrape posts from hashtag pages
        for hashtag in hashtags:
            posts = await scrape_hashtag_posts(page, hashtag, max_posts=9)
            for p in posts:
                if p.shortcode not in seen_shortcodes:
                    seen_shortcodes.add(p.shortcode)
                    all_posts.append(p)
            await random_delay("next hashtag", DELAY_MIN, DELAY_MAX)

        log.info(f"Total unique posts found: {len(all_posts)}")

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
                if await like_ig_post(page, post):
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
                if await comment_ig_post(page, post, comment):
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
                if await follow_ig_user(page, post):
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

        await context.close()

    save_log(eng_log)

    summary = f"IG engage: {likes_done} likes, {comments_done} comments, {follows_done} follows"
    log.info(summary)
    if likes_done + comments_done + follows_done > 0:
        notify("@tatamispaces IG", summary)


if __name__ == "__main__":
    lock_fd = acquire_lock(BASE_DIR / ".ig_engage.lock")
    if not lock_fd:
        log.info("Another ig_engage.py is already running, exiting")
        sys.exit(0)
    try:
        asyncio.run(main())
    finally:
        release_lock(lock_fd)
