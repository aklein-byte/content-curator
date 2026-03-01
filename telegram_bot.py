#!/usr/bin/env python3
"""
Telegram bot for @TatamiSpaces and @MuseumStories.
Conversational AI assistant for post review, approval, and content management.

Usage: python telegram_bot.py
"""

import os
import sys
import json
import logging
from pathlib import Path
from datetime import datetime, timezone

BASE_DIR = Path(__file__).parent
sys.path.insert(0, str(BASE_DIR))

from dotenv import load_dotenv
load_dotenv(BASE_DIR / ".env")

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, ContextTypes, filters,
)

from tools.common import load_json, save_json, setup_logging, get_model
from config.niches import get_niche

log = setup_logging("telegram_bot")

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
MEMORY_FILE = BASE_DIR / "data" / "bot-memory.json"
DECISIONS_LOG = BASE_DIR / "data" / "bot-decisions.json"
MAX_CHAT_HISTORY = 20


# ============================================================
# Auth
# ============================================================

def authorized(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if CHAT_ID and str(update.effective_chat.id) != str(CHAT_ID):
            await update.effective_message.reply_text("Unauthorized.")
            return
        return await func(update, context)
    return wrapper


# ============================================================
# Data helpers
# ============================================================

def _posts_path(niche_id):
    niche = get_niche(niche_id)
    return BASE_DIR / niche.get("posts_file", "posts.json")

def _load_posts(niche_id):
    return load_json(_posts_path(niche_id), default={"posts": []})

def _save_posts(niche_id, data):
    save_json(_posts_path(niche_id), data, lock=True)

def _get_drafts(niche_id):
    data = _load_posts(niche_id)
    return [p for p in data.get("posts", []) if p.get("status") == "draft"]

def _get_post(niche_id, post_id):
    data = _load_posts(niche_id)
    for p in data.get("posts", []):
        if p.get("id") == post_id:
            return p
    return None

def _update_post_status(niche_id, post_id, new_status, **extra):
    data = _load_posts(niche_id)
    for p in data.get("posts", []):
        if p.get("id") == post_id:
            p["status"] = new_status
            if new_status == "approved" and not p.get("scheduled_for"):
                p["scheduled_for"] = datetime.now(timezone.utc).isoformat()
            for k, v in extra.items():
                if v is None:
                    p.pop(k, None)
                else:
                    p[k] = v
            break
    _save_posts(niche_id, data)

def _update_post_field(niche_id, post_id, **fields):
    data = _load_posts(niche_id)
    for p in data.get("posts", []):
        if p.get("id") == post_id:
            for k, v in fields.items():
                if v is None:
                    p.pop(k, None)
                else:
                    p[k] = v
            break
    _save_posts(niche_id, data)

NICHE_MAP = {"t": "tatamispaces", "m": "museumstories"}
NICHE_LABEL = {"tatamispaces": "Tatami", "museumstories": "Museum"}
NICHE_SHORT = {"tatamispaces": "t", "museumstories": "m"}


def _find_post_any_niche(post_id):
    """Find a post by ID across all niches, return (niche_id, post) or (None, None)."""
    for nid in ("tatamispaces", "museumstories"):
        post = _get_post(nid, post_id)
        if post:
            return nid, post
    return None, None


# ============================================================
# Memory & Learning
# ============================================================

def _load_memory():
    return load_json(MEMORY_FILE, default={"preferences": [], "insights": [], "updated_at": None})

def _save_memory(mem):
    mem["updated_at"] = datetime.now(timezone.utc).isoformat()
    save_json(MEMORY_FILE, mem)

def _log_decision(post, action, niche_id, user_text=""):
    decisions = load_json(DECISIONS_LOG, default=[])
    decisions.append({
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "niche": niche_id,
        "post_id": post.get("id"),
        "post_type": post.get("type", ""),
        "action": action,
        "user_text": user_text[:200],
        "category": post.get("category", ""),
        "has_price": bool(post.get("price")),
        "has_source_url": bool(post.get("source_url")),
        "num_images": len(post.get("image_urls", post.get("allImages", []))),
        "caption_length": len(post.get("text", "")),
    })
    if len(decisions) > 500:
        decisions = decisions[-500:]
    save_json(DECISIONS_LOG, decisions)

def _add_to_chat_history(context, role, text):
    history = context.user_data.setdefault("chat_history", [])
    history.append({"role": role, "content": text[:500]})
    if len(history) > MAX_CHAT_HISTORY:
        context.user_data["chat_history"] = history[-MAX_CHAT_HISTORY:]

def _build_preference_summary():
    decisions = load_json(DECISIONS_LOG, default=[])
    if len(decisions) < 5:
        return "Not enough history yet."
    approves = [d for d in decisions if d["action"] == "approve"]
    drops = [d for d in decisions if d["action"] == "drop"]
    lines = [f"{len(decisions)} past decisions ({len(approves)} approved, {len(drops)} dropped)"]
    type_stats = {}
    for d in decisions:
        t = d.get("post_type", "unknown")
        type_stats.setdefault(t, {"approve": 0, "drop": 0})
        if d["action"] in ("approve", "drop"):
            type_stats[t][d["action"]] += 1
    for t, s in type_stats.items():
        total_t = s["approve"] + s["drop"]
        if total_t >= 3:
            rate = s["approve"] / total_t * 100
            lines.append(f"  {t}: {rate:.0f}% approval ({total_t} reviewed)")
    return "\n".join(lines)


# ============================================================
# Claude chat
# ============================================================

async def _chat_with_claude(text, current_post, niche_id, stats, context):
    """Send message to Claude with full context, get structured action back."""
    from anthropic import Anthropic

    post_context = ""
    if current_post:
        info = {k: str(current_post[k])[:500] for k in
                ("id", "type", "text", "title", "artist", "source", "price",
                 "location", "source_url", "category", "medium", "dimensions", "draft_reason")
                if current_post.get(k)}
        imgs = current_post.get("image_urls", current_post.get("allImages", []))
        info["num_images"] = len(imgs)
        post_context = f"\nCurrently reviewing post:\n{json.dumps(info, indent=2, default=str)}"

    stats_text = "\n".join(
        f"  {NICHE_LABEL.get(n, n)}: {s.get('drafts', 0)} drafts, {s.get('approved', 0)} approved, {s.get('posted', 0)} posted"
        for n, s in stats.items()
    )

    reviewing = "tatami" if "tatami_session" in context.user_data else (
        "museum" if "museum_session" in context.user_data else "none")

    memory = _load_memory()
    prefs = memory.get("preferences", [])
    prefs_text = "\n".join(f"  - {p}" for p in prefs[-10:]) if prefs else "  None yet"

    system = f"""You are Tatami Bot, managing two X/Twitter accounts:
1. @TatamiSpaces - Japanese architecture & interior design
2. @MuseumStories - stories behind museum objects

You help review drafts, check stats, and manage content. Be casual and brief.

State:
  Reviewing: {reviewing}
{post_context}
Queue:
{stats_text}
Decision history: {_build_preference_summary()}
Owner preferences:
{prefs_text}

Respond with a JSON object. Actions:
- "approve" - approve post and select images. Optional image fields (0-indexed):
  - "image_indices": [1,2,3] — use ONLY these images (e.g. skip first = [1,2,3])
  - "image_index": 0 — use only this ONE image
  - "image_count": 2 — use only the first N images
- "drop" - reject/delete the entire post from the queue
- "skip" - skip to next post without deciding
- "hold" - save for later
- "regen" - regenerate caption. Optional: "feedback" with direction
- "start_review" - begin reviewing. Required: "niche" ("tatamispaces"|"museumstories")
- "goto" - jump to a specific post. Required: "post_id" (int). Works for any status.
- "undo" - revert last action on a post, set it back to draft. Required: "post_id" (int)
- "learn" - user teaching a preference. Required: "preference" (what to save)
- "chat" - conversation/question

CRITICAL RULES:
1. "drop" means DELETE THE ENTIRE POST. NEVER use "drop" when user says "drop an image" or "remove a pic" — that's image selection, use "approve" with "image_indices".
2. ONLY use "approve"/"drop" when the user gives a CLEAR, UNAMBIGUOUS directive: "ok", "approve it", "drop it", "yes post it", "delete this post".
3. If the user talks about images ("drop the first pic", "skip image 1", "use pics 2-4", "remove pic 1"), that means approve with image selection — use "image_indices" with the 0-indexed images they want to KEEP.
4. If the user is asking a QUESTION ("should we...", "does this have...", "what about...", "can we..."), ALWAYS use "chat".
5. If the message combines a question with something else, use "chat" — answer the question, do NOT take action.
6. "go back to #51", "show me 51" → use "goto" with "post_id": 51
7. "undo 51", "unapprove 51", "bring back 51" → use "undo" with "post_id": 51
8. When in doubt between action and chat, ALWAYS choose "chat".

Image numbering: Users say "pic 1" meaning the first image, but internally images are 0-indexed. "drop pic 1" = keep [1,2,3], "keep pics 2 and 3" = keep [1,2], "use only pic 3" = image_index: 2.

Always include "reply" with a short human message.
If not reviewing a post and user says ok/drop, tell them to start a review.
Reply ONLY with the JSON object."""

    try:
        client = Anthropic()
        messages = list(context.user_data.get("chat_history", []))
        messages.append({"role": "user", "content": text})

        response = client.messages.create(
            model=get_model("chat"),
            max_tokens=300,
            system=system,
            messages=messages,
        )
        result = response.content[0].text.strip()
        if result.startswith("```"):
            result = result.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        return json.loads(result)
    except Exception as e:
        log.error(f"Claude failed: {e}")
        lower = text.lower().strip()
        if current_post:
            pid = current_post.get("id")
            if lower in ("ok", "yes", "y", "approve", "good"):
                return {"action": "approve", "reply": f"Approved #{pid}"}
            if lower in ("no", "drop", "trash", "delete", "nah"):
                return {"action": "drop", "reply": f"Dropped #{pid}"}
            if lower in ("skip", "next", "pass"):
                return {"action": "skip", "reply": f"Skipped #{pid}"}
        return {"action": "chat", "reply": f"Claude unavailable ({e}). Use buttons or /q /qm /stats."}


# ============================================================
# Commands
# ============================================================

@authorized
async def cmd_start(update, context):
    cid = update.effective_chat.id
    await update.message.reply_text(
        f"Hey! I'm your content assistant for @TatamiSpaces and @MuseumStories.\n\n"
        f"Chat ID: {cid}\n\n"
        f"Just talk to me naturally, or use:\n"
        f"/q - Review tatami drafts\n"
        f"/qm - Review museum drafts\n"
        f"/stats - Queue counts\n"
        f"/insights - What's working\n"
        f"/experiments - Content experiments\n"
        f"/learn - Run learning engine now"
    )

@authorized
async def cmd_stats(update, context):
    lines = []
    for niche_id, label in NICHE_LABEL.items():
        data = _load_posts(niche_id)
        posts = data.get("posts", [])
        d = sum(1 for p in posts if p.get("status") == "draft")
        a = sum(1 for p in posts if p.get("status") == "approved")
        po = sum(1 for p in posts if p.get("status") == "posted")
        lines.append(f"{label}: {d} drafts, {a} approved, {po} posted")
    await update.message.reply_text("\n".join(lines))

@authorized
async def cmd_queue(update, context):
    context.user_data["tatami_session"] = {"approved": 0, "dropped": 0}
    context.user_data["tatami_draft_idx"] = 0
    context.user_data.pop("museum_session", None)
    await _show_tatami_draft(update.message, context, 0)

@authorized
async def cmd_queue_museum(update, context):
    context.user_data["museum_session"] = {"approved": 0, "dropped": 0}
    context.user_data["museum_draft_idx"] = 0
    context.user_data.pop("tatami_session", None)
    await _show_museum_draft(update.message, context, 0)

@authorized
async def cmd_skip_regen(update, context):
    regen = context.user_data.get("regen_pending")
    if not regen:
        await update.message.reply_text("Nothing to skip.")
        return
    await _do_regen(update.message, context, regen, feedback=None)

@authorized
async def cmd_insights(update, context):
    lines = []
    for niche_id, label in NICHE_LABEL.items():
        path = BASE_DIR / "data" / f"insights-{niche_id}.json"
        if not path.exists():
            lines.append(f"{label}: No insights yet (run /learn)")
            continue
        ins = load_json(path, default={})
        v = ins.get("version", 0)
        updated = ins.get("updated_at", "?")[:16]
        lines.append(f"{label} (v{v}, {updated}):")
        for w in ins.get("writing_insights", [])[:3]:
            lines.append(f"  {w}")
        for t in ins.get("trending_topics", [])[:2]:
            lines.append(f"  Trending: {t}")
        for r in ins.get("recommendations", [])[:2]:
            lines.append(f"  Rec: {r}")
        props = ins.get("experiment_proposals", [])
        if props:
            lines.append(f"  {len(props)} experiment(s) proposed")
    await update.message.reply_text("\n".join(lines) or "No insights yet.")

@authorized
async def cmd_experiments(update, context):
    exp_data = load_json(BASE_DIR / "data" / "experiments.json", default={"experiments": []})
    exps = exp_data.get("experiments", [])
    if not exps:
        await update.message.reply_text("No experiments yet. Run /learn to generate proposals.")
        return
    lines = []
    for e in exps:
        status = e.get("status", "?")
        niche = e.get("niche", "?")[:3]
        lines.append(f"[{status}] {niche}: {e.get('hypothesis', '?')}")
        if status == "proposed":
            lines.append(f"  ID: {e.get('id')}")
    kb_rows = []
    proposed = [e for e in exps if e.get("status") == "proposed"]
    for e in proposed[:3]:
        eid = e.get("id", "")
        kb_rows.append([
            InlineKeyboardButton(f"Approve {eid}", callback_data=f"ea:{eid[:20]}"),
            InlineKeyboardButton(f"Reject {eid}", callback_data=f"er:{eid[:20]}"),
        ])
    text = "\n".join(lines)
    if kb_rows:
        await update.message.reply_text(text[:4096], reply_markup=InlineKeyboardMarkup(kb_rows))
    else:
        await update.message.reply_text(text[:4096])

@authorized
async def cmd_learn(update, context):
    await update.message.reply_text("Running learning engine...")
    import subprocess
    result = subprocess.run(
        ["/home/amit/tatami-bot/venv/bin/python", "/home/amit/tatami-bot/learn.py"],
        capture_output=True, text=True, timeout=300, cwd="/home/amit/tatami-bot",
    )
    if result.returncode == 0:
        await update.message.reply_text("Learning complete. Use /insights to see results.")
    else:
        err = result.stderr[-500:] if result.stderr else "unknown error"
        await update.message.reply_text(f"Learning failed:\n{err}")


# ============================================================
# Draft display
# ============================================================

async def _show_tatami_draft(msg, context, draft_index=0):
    drafts = _get_drafts("tatamispaces")
    session = context.user_data.setdefault("tatami_session", {"approved": 0, "dropped": 0})

    if draft_index >= len(drafts):
        a, d = session.get("approved", 0), session.get("dropped", 0)
        context.user_data.pop("tatami_session", None)
        await msg.reply_text(f"All caught up! {a} approved, {d} dropped.")
        return

    post = drafts[draft_index]
    pid = post["id"]
    ns = NICHE_SHORT["tatamispaces"]
    images = post.get("image_urls", [])
    caption = post.get("text", "(no text)")
    ptype = post.get("type", "")
    remaining = len(drafts) - draft_index

    text = f"Draft #{pid} ({ptype}) -- {remaining} remaining\n\n{caption}"

    if len(images) > 1:
        rows = [
            [InlineKeyboardButton("Approve All", callback_data=f"a:{ns}:{pid}"),
             InlineKeyboardButton("Drop", callback_data=f"d:{ns}:{pid}")],
            [InlineKeyboardButton("Img 1 only", callback_data=f"is:{ns}:{pid}:0")]
            + ([InlineKeyboardButton("First 2", callback_data=f"ic:{ns}:{pid}:2")] if len(images) >= 2 else [])
            + ([InlineKeyboardButton("First 3", callback_data=f"ic:{ns}:{pid}:3")] if len(images) >= 3 else []),
            [InlineKeyboardButton("Regen", callback_data=f"r:{ns}:{pid}"),
             InlineKeyboardButton("Skip", callback_data=f"s:{ns}:{pid}")],
        ]
    else:
        rows = [
            [InlineKeyboardButton("Approve", callback_data=f"a:{ns}:{pid}"),
             InlineKeyboardButton("Drop", callback_data=f"d:{ns}:{pid}")],
            [InlineKeyboardButton("Regen", callback_data=f"r:{ns}:{pid}"),
             InlineKeyboardButton("Skip", callback_data=f"s:{ns}:{pid}")],
        ]

    kb = InlineKeyboardMarkup(rows)

    if images:
        try:
            if len(images) == 1:
                await msg.reply_photo(images[0], caption=text[:1024], reply_markup=kb)
                return
            else:
                await msg.reply_media_group([InputMediaPhoto(u) for u in images[:4]])
                await msg.reply_text(text[:4096], reply_markup=kb)
                return
        except Exception as e:
            log.warning(f"Image send failed for #{pid}: {e}")

    await msg.reply_text(text[:4096], reply_markup=kb)


async def _show_museum_draft(msg, context, draft_index=0):
    drafts = _get_drafts("museumstories")
    session = context.user_data.setdefault("museum_session", {"approved": 0, "dropped": 0})

    if draft_index >= len(drafts):
        a, d = session.get("approved", 0), session.get("dropped", 0)
        context.user_data.pop("museum_session", None)
        await msg.reply_text(f"All caught up! {a} approved, {d} dropped.")
        return

    post = drafts[draft_index]
    pid = post["id"]
    ns = NICHE_SHORT["museumstories"]
    all_images = post.get("allImages", [])
    tweets = post.get("tweets", [])
    title = post.get("title", "Untitled")
    artist = post.get("artist", "")
    fmt = post.get("format", "single")
    remaining = len(drafts) - draft_index

    if all_images:
        try:
            media = [InputMediaPhoto(url, caption=(f"[0] {title}" if i == 0 else f"[{i}]")[:1024])
                     for i, url in enumerate(all_images[:10])]
            await msg.reply_media_group(media)
        except Exception as e:
            log.warning(f"Museum image send failed for #{pid}: {e}")

    header = f"Museum #{pid}: {title}\n{artist}\nFormat: {fmt} | {remaining} remaining\n\n"
    tweet_texts = []
    for i, tw in enumerate(tweets):
        imgs = tw.get("images", [])
        img_str = ", ".join(str(x) for x in imgs) if imgs else "none"
        tweet_texts.append(f"Tweet {i+1} (imgs: {img_str}):\n{tw.get('text', '')[:300]}")

    body = header + "\n\n".join(tweet_texts)
    if len(body) > 4096:
        body = body[:4093] + "..."

    per_tweet = [[InlineKeyboardButton(f"Assign Imgs T{i+1}", callback_data=f"ma:{ns}:{pid}:{i}"),
                  InlineKeyboardButton(f"Regen T{i+1}", callback_data=f"rt:{ns}:{pid}:{i}")]
                 for i in range(len(tweets))]

    main = [
        [InlineKeyboardButton("Approve", callback_data=f"a:{ns}:{pid}"),
         InlineKeyboardButton("Hold", callback_data=f"h:{ns}:{pid}"),
         InlineKeyboardButton("Reject", callback_data=f"x:{ns}:{pid}")],
        [InlineKeyboardButton("Regen All", callback_data=f"ra:{ns}:{pid}"),
         InlineKeyboardButton("Skip", callback_data=f"s:{ns}:{pid}")],
    ]

    await msg.reply_text(body, reply_markup=InlineKeyboardMarkup(per_tweet + main))


async def _show_single_post(msg, context, niche_id, post):
    """Show any post by ID (regardless of status) with action buttons.
    Also sets up the review session so subsequent messages act on this post."""
    pid = post["id"]
    status = post.get("status", "unknown")

    # Set up session state so the post becomes the "current post"
    if niche_id == "museumstories":
        context.user_data["museum_session"] = context.user_data.get("museum_session", {"approved": 0, "dropped": 0})
        context.user_data["museum_viewing_post"] = pid
        context.user_data.pop("tatami_session", None)
    else:
        context.user_data["tatami_session"] = context.user_data.get("tatami_session", {"approved": 0, "dropped": 0})
        context.user_data["tatami_viewing_post"] = pid
        context.user_data.pop("museum_session", None)

    ns = NICHE_SHORT[niche_id]

    if niche_id == "museumstories":
        # Museum post display
        all_images = post.get("allImages", [])
        tweets = post.get("tweets", [])
        title = post.get("title", "Untitled")
        artist = post.get("artist", "")
        fmt = post.get("format", "single")

        if all_images:
            try:
                media = [InputMediaPhoto(url, caption=(f"[0] {title}" if i == 0 else f"[{i}]")[:1024])
                         for i, url in enumerate(all_images[:10])]
                await msg.reply_media_group(media)
            except Exception as e:
                log.warning(f"Museum image send failed for #{pid}: {e}")

        header = f"Museum #{pid} [{status}]: {title}\n{artist}\nFormat: {fmt}\n\n"
        tweet_texts = []
        for i, tw in enumerate(tweets):
            imgs = tw.get("images", [])
            img_str = ", ".join(str(x) for x in imgs) if imgs else "none"
            tweet_texts.append(f"Tweet {i+1} (imgs: {img_str}):\n{tw.get('text', '')[:300]}")
        body = header + "\n\n".join(tweet_texts)
        if len(body) > 4096:
            body = body[:4093] + "..."

        rows = [
            [InlineKeyboardButton("Approve", callback_data=f"a:{ns}:{pid}"),
             InlineKeyboardButton("Hold", callback_data=f"h:{ns}:{pid}"),
             InlineKeyboardButton("Reject", callback_data=f"x:{ns}:{pid}")],
            [InlineKeyboardButton("Set to Draft", callback_data=f"ud:{ns}:{pid}"),
             InlineKeyboardButton("Skip", callback_data=f"s:{ns}:{pid}")],
        ]
        await msg.reply_text(body, reply_markup=InlineKeyboardMarkup(rows))
    else:
        # Tatami post display
        all_images = post.get("image_urls", [])
        caption = post.get("text", "(no text)")
        ptype = post.get("type", "")

        # Show which images are selected
        sel_indices = post.get("image_indices")
        sel_index = post.get("image_index")
        sel_count = post.get("image_count")
        if sel_indices:
            img_note = f"Images: showing {[i+1 for i in sel_indices]} of {len(all_images)}"
            display_images = [all_images[i] for i in sel_indices if i < len(all_images)]
        elif sel_index is not None:
            img_note = f"Images: showing only #{sel_index+1} of {len(all_images)}"
            display_images = [all_images[sel_index]] if sel_index < len(all_images) else all_images
        elif sel_count is not None:
            img_note = f"Images: showing first {sel_count} of {len(all_images)}"
            display_images = all_images[:sel_count]
        else:
            img_note = f"Images: all {len(all_images)}"
            display_images = all_images

        text = f"Post #{pid} ({ptype}) [{status}]\n{img_note}\n\n{caption}"

        rows = [
            [InlineKeyboardButton("Approve", callback_data=f"a:{ns}:{pid}"),
             InlineKeyboardButton("Drop", callback_data=f"d:{ns}:{pid}")],
            [InlineKeyboardButton("Set to Draft", callback_data=f"ud:{ns}:{pid}"),
             InlineKeyboardButton("Regen", callback_data=f"r:{ns}:{pid}")],
        ]
        kb = InlineKeyboardMarkup(rows)

        if display_images:
            try:
                if len(display_images) == 1:
                    await msg.reply_photo(display_images[0], caption=text[:1024], reply_markup=kb)
                    return
                else:
                    await msg.reply_media_group([InputMediaPhoto(u) for u in display_images[:4]])
                    await msg.reply_text(text[:4096], reply_markup=kb)
                    return
            except Exception as e:
                log.warning(f"Image send failed for #{pid}: {e}")
        await msg.reply_text(text[:4096], reply_markup=kb)


# ============================================================
# Callback handler (buttons)
# ============================================================

@authorized
async def handle_callback(update, context):
    query = update.callback_query
    await query.answer()
    data = query.data
    parts = data.split(":")
    action = parts[0]
    log.info(f"Callback: {data}")

    def _niche(idx=1):
        return NICHE_MAP.get(parts[idx])

    async def _advance(niche_id, action_name, pid):
        """Common: edit message, show next draft."""
        try:
            await query.edit_message_text(f"{action_name} #{pid}")
        except Exception:
            pass
        if niche_id == "tatamispaces":
            context.user_data["tatami_draft_idx"] = 0
            await _show_tatami_draft(query.message, context, 0)
        else:
            context.user_data["museum_draft_idx"] = 0
            await _show_museum_draft(query.message, context, 0)

    # Approve
    if action == "a" and len(parts) == 3:
        nid, pid = _niche(), int(parts[2])
        if not nid: return
        _update_post_status(nid, pid, "approved")
        sk = "tatami_session" if nid == "tatamispaces" else "museum_session"
        s = context.user_data.get(sk, {})
        s["approved"] = s.get("approved", 0) + 1
        context.user_data[sk] = s
        post = _get_post(nid, pid)
        if post: _log_decision(post, "approve", nid)
        await _advance(nid, "Approved", pid)

    # Drop
    elif action == "d" and len(parts) == 3:
        nid, pid = _niche(), int(parts[2])
        if not nid: return
        _update_post_status(nid, pid, "dropped")
        sk = "tatami_session" if nid == "tatamispaces" else "museum_session"
        s = context.user_data.get(sk, {})
        s["dropped"] = s.get("dropped", 0) + 1
        context.user_data[sk] = s
        post = _get_post(nid, pid)
        if post: _log_decision(post, "drop", nid)
        await _advance(nid, "Dropped", pid)

    # Skip
    elif action == "s" and len(parts) == 3:
        nid, pid = _niche(), int(parts[2])
        if not nid: return
        try:
            await query.edit_message_text(f"Skipped #{pid}")
        except Exception:
            pass
        ik = "tatami_draft_idx" if nid == "tatamispaces" else "museum_draft_idx"
        idx = context.user_data.get(ik, 0) + 1
        context.user_data[ik] = idx
        if nid == "tatamispaces":
            await _show_tatami_draft(query.message, context, idx)
        else:
            await _show_museum_draft(query.message, context, idx)

    # Regen
    elif action == "r" and len(parts) == 3:
        nid, pid = _niche(), int(parts[2])
        if not nid: return
        context.user_data["regen_pending"] = {
            "niche_id": nid, "post_id": pid,
            "type": "tatami" if nid == "tatamispaces" else "museum_full",
        }
        await query.edit_message_text(f"Regenerating #{pid}\nSend feedback or /skip for default:")

    # Image select single
    elif action == "is" and len(parts) == 4:
        nid, pid, img = _niche(), int(parts[2]), int(parts[3])
        if not nid: return
        _update_post_field(nid, pid, image_index=img)
        _update_post_status(nid, pid, "approved")
        sk = "tatami_session"
        s = context.user_data.get(sk, {})
        s["approved"] = s.get("approved", 0) + 1
        context.user_data[sk] = s
        post = _get_post(nid, pid)
        if post: _log_decision(post, "approve", nid, f"image {img+1} only")
        await _advance(nid, f"Approved (img {img+1})", pid)

    # Image count first N
    elif action == "ic" and len(parts) == 4:
        nid, pid, count = _niche(), int(parts[2]), int(parts[3])
        if not nid: return
        _update_post_field(nid, pid, image_count=count)
        _update_post_status(nid, pid, "approved")
        sk = "tatami_session"
        s = context.user_data.get(sk, {})
        s["approved"] = s.get("approved", 0) + 1
        context.user_data[sk] = s
        post = _get_post(nid, pid)
        if post: _log_decision(post, "approve", nid, f"first {count}")
        await _advance(nid, f"Approved (first {count})", pid)

    # Hold (museum)
    elif action == "h" and len(parts) == 3:
        nid, pid = _niche(), int(parts[2])
        if not nid: return
        _update_post_status(nid, pid, "hold")
        await _advance(nid, "On hold", pid)

    # Reject (museum)
    elif action == "x" and len(parts) == 3:
        nid, pid = _niche(), int(parts[2])
        if not nid: return
        _update_post_status(nid, pid, "dropped")
        s = context.user_data.get("museum_session", {})
        s["dropped"] = s.get("dropped", 0) + 1
        context.user_data["museum_session"] = s
        post = _get_post(nid, pid)
        if post: _log_decision(post, "drop", nid)
        await _advance(nid, "Rejected", pid)

    # Museum image assignment
    elif action == "ma" and len(parts) == 4:
        nid, pid, tidx = _niche(), int(parts[2]), int(parts[3])
        if not nid: return
        await _show_image_assign(query, nid, pid, tidx)

    elif action in ("ia", "ir") and len(parts) == 5:
        nid, pid, tidx, iidx = _niche(), int(parts[2]), int(parts[3]), int(parts[4])
        if not nid: return
        data = _load_posts(nid)
        for p in data.get("posts", []):
            if p.get("id") == pid:
                tw = p.get("tweets", [])
                if tidx < len(tw):
                    if "images" not in tw[tidx]:
                        tw[tidx]["images"] = []
                    if action == "ia" and iidx not in tw[tidx]["images"]:
                        tw[tidx]["images"].append(iidx)
                    elif action == "ir":
                        tw[tidx]["images"] = [i for i in tw[tidx]["images"] if i != iidx]
                break
        _save_posts(nid, data)
        await _show_image_assign(query, nid, pid, tidx)

    elif action == "md" and len(parts) == 3:
        nid, pid = _niche(), int(parts[2])
        if not nid: return
        try:
            await query.edit_message_text(f"Images saved for #{pid}")
        except Exception:
            pass
        idx = context.user_data.get("museum_draft_idx", 0)
        await _show_museum_draft(query.message, context, idx)

    # Museum regen tweet
    elif action == "rt" and len(parts) == 4:
        nid, pid, tidx = _niche(), int(parts[2]), int(parts[3])
        if not nid: return
        context.user_data["regen_pending"] = {
            "niche_id": nid, "post_id": pid, "tweet_idx": tidx, "type": "museum_tweet",
        }
        await query.edit_message_text(f"Regen tweet {tidx+1} of #{pid}\nSend feedback or /skip:")

    elif action == "ra" and len(parts) == 3:
        nid, pid = _niche(), int(parts[2])
        if not nid: return
        context.user_data["regen_pending"] = {
            "niche_id": nid, "post_id": pid, "type": "museum_full",
        }
        await query.edit_message_text(f"Regen all tweets for #{pid}\nSend feedback or /skip:")

    # Keep as draft (after regen)
    elif action == "kd" and len(parts) == 3:
        try:
            await query.edit_message_text(f"Kept #{parts[2]} as draft")
        except Exception:
            pass

    # Undo / set back to draft
    elif action == "ud" and len(parts) == 3:
        nid, pid = _niche(), int(parts[2])
        if not nid: return
        _update_post_status(nid, pid, "draft", scheduled_for=None)
        try:
            await query.edit_message_text(f"#{pid} set back to draft")
        except Exception:
            pass

    # Experiment approve/reject
    elif action in ("ea", "er") and len(parts) == 2:
        eid = parts[1]
        exp_data = load_json(BASE_DIR / "data" / "experiments.json", default={"experiments": []})
        for e in exp_data.get("experiments", []):
            if e.get("id", "").startswith(eid):
                if action == "ea":
                    e["status"] = "active"
                    e["approved_at"] = datetime.now(timezone.utc).isoformat()
                    msg_text = f"Experiment '{e.get('hypothesis', '')}' activated!"
                else:
                    e["status"] = "rejected"
                    msg_text = f"Experiment '{e.get('hypothesis', '')}' rejected."
                break
        else:
            msg_text = f"Experiment {eid} not found."
        save_json(BASE_DIR / "data" / "experiments.json", exp_data)
        try:
            await query.edit_message_text(msg_text)
        except Exception:
            pass

    # Review queue from notification
    elif action == "rq" and len(parts) == 2:
        nid = _niche(0) if len(parts) > 1 else None
        nid = NICHE_MAP.get(parts[1])
        if nid == "tatamispaces":
            context.user_data["tatami_session"] = {"approved": 0, "dropped": 0}
            context.user_data["tatami_draft_idx"] = 0
            await _show_tatami_draft(query.message, context, 0)
        elif nid == "museumstories":
            context.user_data["museum_session"] = {"approved": 0, "dropped": 0}
            context.user_data["museum_draft_idx"] = 0
            await _show_museum_draft(query.message, context, 0)


async def _show_image_assign(query, niche_id, pid, tweet_idx):
    ns = NICHE_SHORT[niche_id]
    post = _get_post(niche_id, pid)
    if not post: return
    tweets = post.get("tweets", [])
    if tweet_idx >= len(tweets): return
    cur = set(tweets[tweet_idx].get("images", []))
    n_imgs = len(post.get("allImages", []))

    rows = []
    row = []
    for i in range(n_imgs):
        if i in cur:
            row.append(InlineKeyboardButton(f"-{i}", callback_data=f"ir:{ns}:{pid}:{tweet_idx}:{i}"))
        else:
            row.append(InlineKeyboardButton(f"+{i}", callback_data=f"ia:{ns}:{pid}:{tweet_idx}:{i}"))
        if len(row) == 4:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("Done", callback_data=f"md:{ns}:{pid}")])

    imgs_str = ", ".join(str(x) for x in sorted(cur)) if cur else "none"
    await query.edit_message_text(
        f"Tweet {tweet_idx+1} images: [{imgs_str}]\nTap to toggle:",
        reply_markup=InlineKeyboardMarkup(rows),
    )


# ============================================================
# Text handler (conversational AI)
# ============================================================

@authorized
async def handle_text(update, context):
    text = update.message.text.strip()
    msg = update.message
    log.info(f"Message: {text!r}")

    # Regen feedback takes priority
    regen = context.user_data.get("regen_pending")
    if regen:
        await _do_regen(msg, context, regen, text)
        return

    # Build context
    reviewing_tatami = "tatami_session" in context.user_data
    reviewing_museum = "museum_session" in context.user_data
    current_post = None
    niche_id = None

    if reviewing_tatami or reviewing_museum:
        niche_id = "museumstories" if reviewing_museum else "tatamispaces"
        # Check if viewing a specific post (from goto)
        viewing_key = "museum_viewing_post" if reviewing_museum else "tatami_viewing_post"
        viewing_pid = context.user_data.get(viewing_key)
        if viewing_pid:
            current_post = _get_post(niche_id, viewing_pid)
        else:
            idx_key = "museum_draft_idx" if reviewing_museum else "tatami_draft_idx"
            idx = context.user_data.get(idx_key, 0)
            drafts = _get_drafts(niche_id)
            if idx < len(drafts):
                current_post = drafts[idx]

    stats = {}
    for nid in ("tatamispaces", "museumstories"):
        data = _load_posts(nid)
        posts = data.get("posts", [])
        stats[nid] = {
            "drafts": sum(1 for p in posts if p.get("status") == "draft"),
            "approved": sum(1 for p in posts if p.get("status") == "approved"),
            "posted": sum(1 for p in posts if p.get("status") == "posted"),
        }

    parsed = await _chat_with_claude(text, current_post, niche_id, stats, context)
    action = parsed.get("action", "chat")
    log.info(f"Action: {action}")

    if action in ("approve", "drop", "hold") and current_post:
        pid = current_post["id"]
        sk = "museum_session" if reviewing_museum else "tatami_session"
        ik = "museum_draft_idx" if reviewing_museum else "tatami_draft_idx"

        if action == "approve":
            if parsed.get("image_indices") is not None:
                _update_post_field(niche_id, pid, image_indices=parsed["image_indices"],
                                   image_index=None, image_count=None)
            elif parsed.get("image_index") is not None:
                _update_post_field(niche_id, pid, image_index=parsed["image_index"],
                                   image_indices=None, image_count=None)
            elif parsed.get("image_count") is not None:
                _update_post_field(niche_id, pid, image_count=parsed["image_count"],
                                   image_indices=None, image_index=None)
            _update_post_status(niche_id, pid, "approved")
            s = context.user_data.get(sk, {})
            s["approved"] = s.get("approved", 0) + 1
            context.user_data[sk] = s
        elif action == "drop":
            _update_post_status(niche_id, pid, "dropped")
            s = context.user_data.get(sk, {})
            s["dropped"] = s.get("dropped", 0) + 1
            context.user_data[sk] = s
        elif action == "hold":
            _update_post_status(niche_id, pid, "hold")

        _log_decision(current_post, action, niche_id, text)
        _add_to_chat_history(context, "user", text)
        reply = parsed.get("reply", f"{action.title()}d #{pid}")
        _add_to_chat_history(context, "assistant", reply)
        await msg.reply_text(reply)

        # Clear viewing state, advance to next draft
        context.user_data.pop("tatami_viewing_post", None)
        context.user_data.pop("museum_viewing_post", None)
        context.user_data[ik] = 0
        if reviewing_museum:
            await _show_museum_draft(msg, context, 0)
        else:
            await _show_tatami_draft(msg, context, 0)

    elif action == "skip" and current_post:
        ik = "museum_draft_idx" if reviewing_museum else "tatami_draft_idx"
        idx = context.user_data.get(ik, 0) + 1
        context.user_data[ik] = idx
        _add_to_chat_history(context, "user", text)
        reply = parsed.get("reply", f"Skipped #{current_post['id']}")
        _add_to_chat_history(context, "assistant", reply)
        await msg.reply_text(reply)
        if reviewing_museum:
            await _show_museum_draft(msg, context, idx)
        else:
            await _show_tatami_draft(msg, context, idx)

    elif action == "regen" and current_post:
        feedback = parsed.get("feedback")
        ri = {"niche_id": niche_id, "post_id": current_post["id"],
              "type": "tatami" if niche_id == "tatamispaces" else "museum_full"}
        if feedback:
            await _do_regen(msg, context, ri, feedback)
        else:
            context.user_data["regen_pending"] = ri
            await msg.reply_text(parsed.get("reply", "Send feedback or /skip for default:"))

    elif action == "start_review":
        target = parsed.get("niche", "tatamispaces")
        _add_to_chat_history(context, "user", text)
        context.user_data.pop("tatami_viewing_post", None)
        context.user_data.pop("museum_viewing_post", None)
        if target == "museumstories":
            context.user_data["museum_session"] = {"approved": 0, "dropped": 0}
            context.user_data["museum_draft_idx"] = 0
            context.user_data.pop("tatami_session", None)
            await _show_museum_draft(msg, context, 0)
        else:
            context.user_data["tatami_session"] = {"approved": 0, "dropped": 0}
            context.user_data["tatami_draft_idx"] = 0
            context.user_data.pop("museum_session", None)
            await _show_tatami_draft(msg, context, 0)

    elif action == "goto":
        target_pid = parsed.get("post_id")
        if target_pid is not None:
            target_pid = int(target_pid)
            # Try current niche first, then search all
            found_niche, found_post = None, None
            if niche_id:
                found_post = _get_post(niche_id, target_pid)
                if found_post:
                    found_niche = niche_id
            if not found_post:
                found_niche, found_post = _find_post_any_niche(target_pid)

            if found_post:
                _add_to_chat_history(context, "user", text)
                reply = parsed.get("reply", f"Showing post #{target_pid}")
                _add_to_chat_history(context, "assistant", reply)
                await msg.reply_text(reply)
                await _show_single_post(msg, context, found_niche, found_post)
            else:
                await msg.reply_text(f"Post #{target_pid} not found in any queue.")
        else:
            await msg.reply_text("Which post? Give me a post ID number.")

    elif action == "undo":
        target_pid = parsed.get("post_id")
        if target_pid is not None:
            target_pid = int(target_pid)
            found_niche, found_post = None, None
            if niche_id:
                found_post = _get_post(niche_id, target_pid)
                if found_post:
                    found_niche = niche_id
            if not found_post:
                found_niche, found_post = _find_post_any_niche(target_pid)

            if found_post:
                old_status = found_post.get("status")
                _update_post_status(found_niche, target_pid, "draft", scheduled_for=None)
                _add_to_chat_history(context, "user", text)
                reply = parsed.get("reply", f"#{target_pid} reverted from {old_status} to draft")
                _add_to_chat_history(context, "assistant", reply)
                await msg.reply_text(reply)
                # Show it again for re-review
                found_post = _get_post(found_niche, target_pid)
                if found_post:
                    await _show_single_post(msg, context, found_niche, found_post)
            else:
                await msg.reply_text(f"Post #{target_pid} not found.")
        else:
            await msg.reply_text("Which post? Give me a post ID number.")

    elif action == "learn":
        pref = parsed.get("preference", text)
        mem = _load_memory()
        mem.setdefault("preferences", []).append(pref)
        mem["preferences"] = list(dict.fromkeys(mem["preferences"]))[-20:]
        _save_memory(mem)
        _add_to_chat_history(context, "user", text)
        reply = parsed.get("reply", "Got it, I'll remember that.")
        _add_to_chat_history(context, "assistant", reply)
        await msg.reply_text(reply)

    else:
        _add_to_chat_history(context, "user", text)
        reply = parsed.get("reply", "Not sure what you mean. Try 'review tatami' or 'stats'.")
        _add_to_chat_history(context, "assistant", reply)
        await msg.reply_text(reply)


# ============================================================
# Regen
# ============================================================

async def _do_regen(msg, context, regen, feedback):
    context.user_data.pop("regen_pending", None)
    niche_id = regen["niche_id"]
    pid = regen["post_id"]
    rtype = regen["type"]
    default = "Try a different angle. Keep the same facts but find a fresher way to say it."

    await msg.reply_text("Regenerating...")

    try:
        from agents.writer import rewrite_caption

        if rtype in ("tatami", "museum_full"):
            post = _get_post(niche_id, pid)
            if not post:
                await msg.reply_text("Post not found.")
                return
            original = post.get("text", "")
            if not original and post.get("tweets"):
                original = post["tweets"][0].get("text", "")

            result = await rewrite_caption(niche_id, original, feedback or default)
            new_caption = result["caption"]

            if rtype == "tatami":
                _update_post_field(niche_id, pid, text=new_caption, _previous_text=original)
            else:
                data = _load_posts(niche_id)
                for p in data.get("posts", []):
                    if p.get("id") == pid:
                        p["_previous_text"] = p.get("text", "")
                        p["text"] = new_caption
                        if p.get("tweets"):
                            p["tweets"][0]["text"] = new_caption
                        break
                _save_posts(niche_id, data)

        elif rtype == "museum_tweet":
            tidx = regen.get("tweet_idx", 0)
            post = _get_post(niche_id, pid)
            if not post or tidx >= len(post.get("tweets", [])):
                await msg.reply_text("Tweet not found.")
                return
            original = post["tweets"][tidx].get("text", "")
            result = await rewrite_caption(niche_id, original, feedback or default)
            new_caption = result["caption"]

            data = _load_posts(niche_id)
            for p in data.get("posts", []):
                if p.get("id") == pid:
                    p["tweets"][tidx]["_previous_text"] = original
                    p["tweets"][tidx]["text"] = new_caption
                    break
            _save_posts(niche_id, data)
        else:
            await msg.reply_text("Unknown regen type.")
            return

        ns = NICHE_SHORT[niche_id]
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("Keep & Approve", callback_data=f"a:{ns}:{pid}"),
             InlineKeyboardButton("Keep as Draft", callback_data=f"kd:{ns}:{pid}")],
            [InlineKeyboardButton("Regen Again", callback_data=f"r:{ns}:{pid}")],
        ])
        await msg.reply_text(f"New caption:\n\n{new_caption}", reply_markup=kb)

    except Exception as e:
        log.error(f"Regen failed: {e}", exc_info=True)
        await msg.reply_text(f"Regen failed: {e}")


# ============================================================
# Draft watcher (proactive push)
# ============================================================

_last_mtime = {}

async def check_for_new_drafts(context):
    global _last_mtime
    if not CHAT_ID:
        return

    for niche_id in ("tatamispaces", "museumstories"):
        path = _posts_path(niche_id)
        if not path.exists():
            continue

        mtime = path.stat().st_mtime
        prev = _last_mtime.get(niche_id, 0)
        if prev == 0:
            _last_mtime[niche_id] = mtime
            continue
        if mtime <= prev:
            continue
        _last_mtime[niche_id] = mtime

        drafts = _get_drafts(niche_id)
        prev_count = context.bot_data.get(f"draft_count_{niche_id}", 0)
        current_count = len(drafts)

        if current_count > prev_count and prev_count > 0:
            new_count = current_count - prev_count
            label = NICHE_LABEL.get(niche_id, niche_id)
            ns = NICHE_SHORT[niche_id]
            newest = drafts[-1]
            pid = newest["id"]
            caption = newest.get("text", "(no text)")[:300]
            images = newest.get("image_urls", newest.get("allImages", []))

            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("Approve", callback_data=f"a:{ns}:{pid}"),
                 InlineKeyboardButton("Drop", callback_data=f"d:{ns}:{pid}")],
                [InlineKeyboardButton(f"Review all ({current_count})", callback_data=f"rq:{ns}")],
            ])

            try:
                if images:
                    await context.bot.send_photo(
                        chat_id=int(CHAT_ID), photo=images[0],
                        caption=f"New {label}: {new_count} draft(s)\n\n{caption}"[:1024],
                        reply_markup=kb)
                else:
                    await context.bot.send_message(
                        chat_id=int(CHAT_ID),
                        text=f"New {label}: {new_count} draft(s)\n\n{caption}"[:4096],
                        reply_markup=kb)
            except Exception as e:
                log.warning(f"Push notification failed: {e}")

        context.bot_data[f"draft_count_{niche_id}"] = current_count


# ============================================================
# Main
# ============================================================

def main():
    if not TOKEN:
        log.error("TELEGRAM_BOT_TOKEN not set")
        sys.exit(1)

    # Initialize memory file if it doesn't exist
    if not MEMORY_FILE.exists():
        MEMORY_FILE.parent.mkdir(parents=True, exist_ok=True)
        _save_memory({"preferences": [], "insights": []})
        log.info(f"Created {MEMORY_FILE}")

    log.info(f"Starting bot (chat_id: {CHAT_ID or 'NONE'})")

    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("q", cmd_queue))
    app.add_handler(CommandHandler("qm", cmd_queue_museum))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("skip", cmd_skip_regen))
    app.add_handler(CommandHandler("insights", cmd_insights))
    app.add_handler(CommandHandler("experiments", cmd_experiments))
    app.add_handler(CommandHandler("learn", cmd_learn))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    app.job_queue.run_repeating(check_for_new_drafts, interval=30, first=10)

    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
