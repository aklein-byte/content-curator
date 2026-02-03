"""
Content Curator - Autonomous Marketing System

FastAPI application with Telegram bot integration.
Deploy on Render for long-running agent operations.
"""

import os
import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
from dotenv import load_dotenv

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

from agents.curator import find_images, curate_with_conversation
from agents.writer import write_caption
from tools.storage import (
    init_db,
    get_pending_candidates,
    get_candidate,
    approve_candidate,
    reject_candidate,
    add_to_approved,
    get_ready_to_post,
    mark_as_posted,
    get_stats,
)
from tools.social import post_to_x, format_tweet, verify_credentials
from config.niches import get_niche, list_niches
from config.context import CHAT_SYSTEM_PROMPT

# Load environment variables
load_dotenv()

# Configure logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# Environment
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
ALLOWED_USERS = [
    int(uid.strip())
    for uid in os.environ.get("ALLOWED_TELEGRAM_USERS", "").split(",")
    if uid.strip()
]
RENDER_EXTERNAL_URL = os.environ.get("RENDER_EXTERNAL_URL", "")

# Default niche (can be changed per user in future)
DEFAULT_NICHE = "tatamispaces"

# Telegram application (initialized at startup)
telegram_app: Optional[Application] = None

# Anthropic client for chat
from anthropic import Anthropic
anthropic_client = Anthropic()

# Chat history per user (in-memory, resets on restart)
chat_histories: dict[int, list] = {}


def is_authorized(user_id: int) -> bool:
    """Check if user is authorized to use the bot."""
    if not ALLOWED_USERS:
        return True  # No restrictions if not configured
    return user_id in ALLOWED_USERS


# --- Telegram Handlers ---

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start command."""
    if not is_authorized(update.effective_user.id):
        await update.message.reply_text("Unauthorized. Contact admin for access.")
        return

    await update.message.reply_text(
        "🎨 Content Curator Bot\n\n"
        "Just chat with me - I'm Opus 4.5, your curator.\n\n"
        "Commands:\n"
        "/find [query] - Find new images\n"
        "/review - Review pending candidates\n"
        "/queue - Show approved queue\n"
        "/post - Post next in queue\n"
        "/stats - Show statistics\n"
        "/clear - Clear chat history\n"
        "/help - Show this help\n\n"
        "Or just send me a message to chat!"
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /help command."""
    if not is_authorized(update.effective_user.id):
        return

    await update.message.reply_text(
        "📖 Content Curator Help\n\n"
        "Workflow:\n"
        "1. /find - Agent discovers images\n"
        "2. /review - You approve/reject\n"
        "3. /post - Post to X\n\n"
        "The curator uses Opus 4.5 for taste.\n"
        "Writer uses Sonnet for captions.\n\n"
        "Tips:\n"
        "• /find wabi-sabi interiors\n"
        "• /find [url] to scrape specific site\n"
        "• Approve images you love, reject generics"
    )


async def find_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /find command - discover new images."""
    if not is_authorized(update.effective_user.id):
        return

    query = " ".join(context.args) if context.args else None

    await update.message.reply_text(
        f"🔍 Searching{'...' if not query else f' for: {query}'}\n"
        "This may take a moment (using Opus 4.5 for curation)..."
    )

    try:
        # Check if query is a URL
        source_url = None
        search_query = query
        if query and (query.startswith("http://") or query.startswith("https://")):
            source_url = query
            search_query = None

        images = await find_images(
            niche_id=DEFAULT_NICHE,
            search_query=search_query,
            source_url=source_url,
            count=5,
        )

        if not images:
            await update.message.reply_text(
                "No images passed the quality bar.\n"
                "Try a different query or source."
            )
            return

        await update.message.reply_text(
            f"✨ Found {len(images)} quality images!\n\n"
            f"Use /review to see and approve them."
        )

        # Show first image preview
        for img in images[:1]:
            await update.message.reply_text(
                f"Preview:\n"
                f"📷 Score: {img.quality_score}/10\n"
                f"📍 Source: {img.source_name}\n"
                f"💭 {img.scroll_stop_factor[:200]}"
            )

    except Exception as e:
        logger.error(f"Find error: {e}")
        await update.message.reply_text(f"Error during search: {str(e)[:100]}")


async def review_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /review command - show pending candidates."""
    if not is_authorized(update.effective_user.id):
        return

    candidates = await get_pending_candidates(DEFAULT_NICHE, limit=5)

    if not candidates:
        await update.message.reply_text(
            "No pending candidates.\n"
            "Use /find to discover new images."
        )
        return

    await update.message.reply_text(f"📋 {len(candidates)} pending for review:")

    for candidate in candidates:
        # Create approve/reject buttons
        keyboard = [
            [
                InlineKeyboardButton("✅ Approve", callback_data=f"approve_{candidate['id']}"),
                InlineKeyboardButton("❌ Reject", callback_data=f"reject_{candidate['id']}"),
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        text = (
            f"#{candidate['id']} | Score: {candidate['quality_score']}/10\n"
            f"Source: {candidate['source_name']}\n\n"
            f"{candidate['curator_notes'][:300] if candidate['curator_notes'] else 'No notes'}"
        )

        await update.message.reply_text(text, reply_markup=reply_markup)


async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle button callbacks for approve/reject."""
    query = update.callback_query
    await query.answer()

    if not is_authorized(update.effective_user.id):
        return

    data = query.data

    if data.startswith("approve_"):
        candidate_id = int(data.replace("approve_", ""))
        await handle_approve(query, candidate_id)

    elif data.startswith("reject_"):
        candidate_id = int(data.replace("reject_", ""))
        await handle_reject(query, candidate_id)

    elif data.startswith("confirm_post_"):
        approved_id = int(data.replace("confirm_post_", ""))
        await handle_post(query, approved_id)


async def handle_approve(query, candidate_id: int):
    """Handle approval of a candidate."""
    candidate = await get_candidate(candidate_id)
    if not candidate:
        await query.edit_message_text("Candidate not found.")
        return

    # Approve the candidate
    await approve_candidate(candidate_id)

    # Generate caption
    await query.edit_message_text("✅ Approved! Generating caption...")

    result = await write_caption(
        niche_id=DEFAULT_NICHE,
        image_context=candidate.get("title", "") or candidate.get("description", ""),
        source_name=candidate.get("source_name"),
        curator_notes=candidate.get("curator_notes"),
    )

    caption = result["caption"]
    hashtags = " ".join(result["hashtags"])

    # Add to approved queue
    await add_to_approved(
        candidate_id=candidate_id,
        caption=caption,
        hashtags=hashtags,
    )

    await query.edit_message_text(
        f"✅ Approved and captioned!\n\n"
        f"Caption: {caption}\n"
        f"Hashtags: {hashtags}\n\n"
        f"Use /post to publish."
    )


async def handle_reject(query, candidate_id: int):
    """Handle rejection of a candidate."""
    await reject_candidate(candidate_id, "Manual rejection")
    await query.edit_message_text("❌ Rejected and removed from queue.")


async def queue_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /queue command - show approved posts ready to go."""
    if not is_authorized(update.effective_user.id):
        return

    ready = await get_ready_to_post(DEFAULT_NICHE, limit=5)

    if not ready:
        await update.message.reply_text(
            "No posts in queue.\n"
            "Approve some candidates first with /review"
        )
        return

    await update.message.reply_text(f"📤 {len(ready)} posts ready:")

    for post in ready:
        keyboard = [
            [InlineKeyboardButton("🚀 Post Now", callback_data=f"confirm_post_{post['id']}")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await update.message.reply_text(
            f"#{post['id']}\n{post['caption']}\n\n{post['hashtags']}",
            reply_markup=reply_markup,
        )


async def handle_post(query, approved_id: int):
    """Handle posting to X."""
    ready = await get_ready_to_post(DEFAULT_NICHE, limit=10)
    post = next((p for p in ready if p["id"] == approved_id), None)

    if not post:
        await query.edit_message_text("Post not found or already posted.")
        return

    await query.edit_message_text("📤 Posting to X...")

    # Format tweet
    tweet_text = format_tweet(post["caption"], post.get("hashtags", "").split())

    # Post to X
    result = await post_to_x(
        text=tweet_text,
        image_url=post["image_url"],
    )

    if result.success:
        await mark_as_posted(approved_id, "x", result.post_id)
        await query.edit_message_text(
            f"✅ Posted!\n{result.post_url}"
        )
    else:
        await query.edit_message_text(
            f"❌ Failed to post: {result.error}"
        )


async def post_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /post command - post next in queue."""
    if not is_authorized(update.effective_user.id):
        return

    ready = await get_ready_to_post(DEFAULT_NICHE, limit=1)

    if not ready:
        await update.message.reply_text(
            "No posts in queue.\n"
            "Approve some candidates first with /review"
        )
        return

    post = ready[0]

    keyboard = [
        [InlineKeyboardButton("🚀 Confirm Post", callback_data=f"confirm_post_{post['id']}")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(
        f"Ready to post:\n\n{post['caption']}\n\n{post['hashtags']}",
        reply_markup=reply_markup,
    )


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /stats command - show statistics."""
    if not is_authorized(update.effective_user.id):
        return

    stats = await get_stats(DEFAULT_NICHE)

    # Verify X credentials
    x_status = await verify_credentials()

    text = (
        f"📊 {DEFAULT_NICHE} Stats\n\n"
        f"Candidates:\n"
        f"  • Pending: {stats['candidates'].get('pending', 0)}\n"
        f"  • Approved: {stats['candidates'].get('approved', 0)}\n"
        f"  • Rejected: {stats['candidates'].get('rejected', 0)}\n\n"
        f"Queue:\n"
        f"  • Ready to post: {stats['approved'].get('pending', 0)}\n"
        f"  • Posted: {stats['approved'].get('posted', 0)}\n\n"
        f"Total posted: {stats['posted_total']}\n\n"
        f"X Account: "
    )

    if x_status.get("valid"):
        text += f"@{x_status['username']} ({x_status['followers']} followers)"
    else:
        text += f"Not connected ({x_status.get('error', 'unknown')})"

    await update.message.reply_text(text)


async def niches_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /niches command - list available niches."""
    if not is_authorized(update.effective_user.id):
        return

    niches = list_niches()
    text = "🎨 Available Niches:\n\n"

    for niche_id in niches:
        niche = get_niche(niche_id)
        marker = "✓" if niche_id == DEFAULT_NICHE else " "
        text += f"{marker} {niche['handle']}: {niche['description']}\n"

    text += f"\nCurrently using: {DEFAULT_NICHE}"
    await update.message.reply_text(text)


async def clear_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /clear command - clear chat history."""
    if not is_authorized(update.effective_user.id):
        return

    user_id = update.effective_user.id
    chat_histories[user_id] = []
    await update.message.reply_text("Chat history cleared. Fresh start!")


async def chat_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle free-form chat messages - talk to the curator agent."""
    if not is_authorized(update.effective_user.id):
        return

    user_id = update.effective_user.id
    user_message = update.message.text

    # Initialize chat history for this user if needed
    if user_id not in chat_histories:
        chat_histories[user_id] = []

    # Add user message to history
    chat_histories[user_id].append({
        "role": "user",
        "content": user_message
    })

    # Keep history reasonable (last 20 messages)
    if len(chat_histories[user_id]) > 20:
        chat_histories[user_id] = chat_histories[user_id][-20:]

    # Send typing indicator
    await update.message.chat.send_action("typing")

    try:
        # Call Opus for response
        response = anthropic_client.messages.create(
            model="claude-opus-4-5-20251101",
            max_tokens=1024,
            system=CHAT_SYSTEM_PROMPT,
            messages=chat_histories[user_id],
        )

        assistant_message = response.content[0].text

        # Add assistant response to history
        chat_histories[user_id].append({
            "role": "assistant",
            "content": assistant_message
        })

        # Send response (split if too long for Telegram)
        if len(assistant_message) > 4000:
            # Split into chunks
            for i in range(0, len(assistant_message), 4000):
                await update.message.reply_text(assistant_message[i:i+4000])
        else:
            await update.message.reply_text(assistant_message)

    except Exception as e:
        logger.error(f"Chat error: {e}")
        await update.message.reply_text(f"Error: {str(e)[:200]}")


# --- FastAPI Application ---

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan handler."""
    global telegram_app

    # Initialize database
    await init_db()
    logger.info("Database initialized")

    # Initialize Telegram bot
    if TELEGRAM_BOT_TOKEN:
        telegram_app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

        # Add handlers
        telegram_app.add_handler(CommandHandler("start", start_command))
        telegram_app.add_handler(CommandHandler("help", help_command))
        telegram_app.add_handler(CommandHandler("find", find_command))
        telegram_app.add_handler(CommandHandler("review", review_command))
        telegram_app.add_handler(CommandHandler("queue", queue_command))
        telegram_app.add_handler(CommandHandler("post", post_command))
        telegram_app.add_handler(CommandHandler("stats", stats_command))
        telegram_app.add_handler(CommandHandler("niches", niches_command))
        telegram_app.add_handler(CommandHandler("clear", clear_command))
        telegram_app.add_handler(CallbackQueryHandler(callback_handler))
        # Chat handler for non-command messages (must be last)
        telegram_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, chat_handler))

        # Set webhook
        if RENDER_EXTERNAL_URL:
            webhook_url = f"{RENDER_EXTERNAL_URL}/webhook"
            await telegram_app.initialize()
            await telegram_app.bot.set_webhook(webhook_url)
            logger.info(f"Telegram webhook set to {webhook_url}")
        else:
            # For local development, use polling
            await telegram_app.initialize()
            await telegram_app.start()
            await telegram_app.updater.start_polling()
            logger.info("Telegram polling started")

    yield

    # Cleanup
    if telegram_app:
        if RENDER_EXTERNAL_URL:
            await telegram_app.bot.delete_webhook()
        else:
            await telegram_app.updater.stop()
            await telegram_app.stop()
        await telegram_app.shutdown()


app = FastAPI(title="Content Curator", lifespan=lifespan)


@app.get("/health")
async def health_check():
    """Health check endpoint for Render."""
    return {"status": "healthy", "niche": DEFAULT_NICHE}


@app.post("/webhook")
async def telegram_webhook(request: Request):
    """Handle Telegram webhook updates."""
    if not telegram_app:
        raise HTTPException(status_code=503, detail="Bot not initialized")

    try:
        data = await request.json()
        update = Update.de_json(data, telegram_app.bot)
        await telegram_app.process_update(update)
        return {"ok": True}
    except Exception as e:
        logger.error(f"Webhook error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/")
async def root():
    """Root endpoint."""
    return {
        "name": "Content Curator",
        "status": "running",
        "niche": DEFAULT_NICHE,
        "endpoints": {
            "/health": "Health check",
            "/webhook": "Telegram webhook (POST)",
        },
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
