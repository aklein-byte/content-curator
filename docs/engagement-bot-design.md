# Engagement Bot Architecture

## Overview
Automated system that finds relevant content on X, drafts replies/engagement, and posts approved content on a schedule. Designed to be niche-agnostic — same bot works for @tatamispaces, @brutalist_now, or any future account.

## Core Library: Twikit
Using [twikit](https://github.com/d60/twikit) instead of Tweepy. No API key needed, no $200/mo cost. Uses session cookies to interact with X as a logged-in user.

### What Twikit Can Do
- Post tweets (text + images)
- Search tweets (keyword, hashtag, user)
- Like, retweet, quote-tweet
- Reply to tweets
- Follow/unfollow users
- Get user timelines
- Upload media

## New Files

```
content-curator/
├── agents/
│   ├── curator.py          # (existing) Finds images
│   ├── writer.py           # (existing) Writes captions
│   └── engager.py          # NEW — Drafts replies, evaluates posts
│
├── tools/
│   ├── storage.py          # (existing, extend) Add engagement tables
│   ├── social.py           # (existing) Keep for legacy Tweepy
│   ├── xkit.py             # NEW — Twikit wrapper for X operations
│   └── firecrawl.py        # (existing) Web scraping
│
├── config/
│   ├── niches.py           # (existing, extend) Add engagement config per niche
│   ├── sources.py          # (existing)
│   ├── voice.md            # (existing) Writing style guide
│   └── context.py          # (existing)
│
├── jobs/
│   ├── research.py         # NEW — Daily content discovery job
│   ├── engage.py           # NEW — Daily engagement job
│   └── post.py             # NEW — Scheduled posting job
│
└── main.py                 # (existing, extend) Add cron triggers
```

## Database Extensions

```sql
-- Accounts we're tracking / engaging with
CREATE TABLE IF NOT EXISTS tracked_accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    niche TEXT NOT NULL,
    platform TEXT DEFAULT 'x',
    handle TEXT NOT NULL,
    display_name TEXT,
    follower_count INTEGER,
    category TEXT,  -- 'architect', 'photographer', 'publication', 'curator'
    language TEXT DEFAULT 'ja',
    last_checked_at TIMESTAMP,
    notes TEXT,
    UNIQUE(niche, platform, handle)
);

-- Posts we've found and may want to engage with
CREATE TABLE IF NOT EXISTS discovered_posts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    niche TEXT NOT NULL,
    platform TEXT DEFAULT 'x',
    post_id TEXT NOT NULL,
    author_handle TEXT NOT NULL,
    content_text TEXT,
    image_urls TEXT,  -- JSON array
    likes INTEGER DEFAULT 0,
    reposts INTEGER DEFAULT 0,
    replies INTEGER DEFAULT 0,
    language TEXT,
    discovered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    relevance_score INTEGER,  -- 1-10 from engager agent
    status TEXT DEFAULT 'discovered',  -- discovered/draft/approved/engaged/skipped
    UNIQUE(niche, platform, post_id)
);

-- Our engagement actions (replies, likes, reposts)
CREATE TABLE IF NOT EXISTS engagement_actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    niche TEXT NOT NULL,
    discovered_post_id INTEGER REFERENCES discovered_posts(id),
    action_type TEXT NOT NULL,  -- 'reply', 'like', 'repost', 'quote', 'follow'
    draft_text TEXT,  -- For replies/quotes, the drafted text
    status TEXT DEFAULT 'draft',  -- draft/approved/posted/failed
    drafted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    approved_at TIMESTAMP,
    posted_at TIMESTAMP,
    post_id TEXT  -- The ID of our reply/quote on X
);

-- Scheduled original posts
CREATE TABLE IF NOT EXISTS scheduled_posts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    niche TEXT NOT NULL,
    content_text TEXT NOT NULL,
    image_urls TEXT,  -- JSON array of local paths or URLs to download
    credit_handle TEXT,  -- Original creator to credit
    source_post_url TEXT,  -- Where we found it
    scheduled_for TIMESTAMP,
    status TEXT DEFAULT 'draft',  -- draft/approved/scheduled/posted/failed
    post_id TEXT,  -- X post ID after posting
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    posted_at TIMESTAMP
);
```

## Niche Config Extension (niches.py)

```python
# Add to each niche definition:
"engagement": {
    "search_queries": [
        # Japanese language searches
        "(畳 OR 障子 OR 縁側 OR 茶室) filter:images min_faves:200 lang:ja",
        "(古民家 OR 町家) filter:images min_faves:100 lang:ja",
        "(建築 OR インテリア) filter:images min_faves:500 lang:ja",
        # English searches
        "japanese interior design filter:images min_faves:100",
        "japanese architecture filter:images min_faves:200",
    ],
    "tracked_accounts": [
        "@naparchitects", "@WalkAroundTokyo", "@530E",
        "@kenohare_media", "@Hasselblad_JPN", "@JapanArchitects",
    ],
    "reply_voice": "Knowledgeable but casual. Add context the English-speaking audience wouldn't know. Use specific details from voice.md.",
    "engagement_targets": {
        "replies_per_day": 5,
        "likes_per_day": 10,
        "reposts_per_day": 2,
        "original_posts_per_day": 2,
    },
    "posting_times": [
        "09:00",  # Morning US East
        "13:00",  # Lunch US East / Morning US West
        "19:00",  # Evening US East
    ],
}
```

## agents/engager.py — The Engagement Agent

```python
"""
Engager Agent — Drafts replies and evaluates posts for engagement.
Uses Claude to generate contextual, on-brand replies.
Reads voice.md for style guidance.
"""

ENGAGER_MODEL = "claude-sonnet-4-20250514"  # Sonnet is fine for replies

async def evaluate_post(post_text: str, author: str, niche_id: str) -> dict:
    """
    Evaluate whether a discovered post is worth engaging with.
    Returns: { relevance_score: 1-10, should_engage: bool, reason: str }
    """

async def draft_reply(post_text: str, author: str, niche_id: str) -> str:
    """
    Draft a reply to a post using the niche's voice and style.
    Loads voice.md as context.
    Returns draft reply text.
    """

async def draft_original_post(
    source_post_url: str,
    source_text: str,
    author: str,
    image_description: str,
    niche_id: str,
) -> dict:
    """
    Draft an original post (download + repost with credit).
    Returns: { text: str, credit: str }
    """
```

## tools/xkit.py — Twikit Wrapper

```python
"""
X/Twitter operations via Twikit (no API key needed).
Wraps twikit for our specific use cases.
Niche-agnostic — takes account credentials as config.
"""

from twikit import Client

async def login(username: str, email: str, password: str) -> Client:
    """Login and cache session cookies."""

async def search_posts(client: Client, query: str, count: int = 20) -> list:
    """Search X for posts matching query."""

async def post_tweet(client: Client, text: str, image_paths: list = None) -> str:
    """Post a tweet with optional images. Returns post ID."""

async def reply_to_post(client: Client, post_id: str, text: str) -> str:
    """Reply to an existing post. Returns reply ID."""

async def like_post(client: Client, post_id: str):
    """Like a post."""

async def repost(client: Client, post_id: str):
    """Repost/retweet a post."""

async def download_image(url: str, save_path: str) -> str:
    """Download image from URL to local path."""

async def get_user_posts(client: Client, handle: str, count: int = 20) -> list:
    """Get recent posts from a user."""
```

## jobs/research.py — Daily Content Discovery

```python
"""
Daily cron job: Find content worth posting or engaging with.
Runs search queries for the niche, evaluates posts, stores candidates.
"""

async def run_research(niche_id: str):
    """
    1. Login to X via twikit
    2. Run each search query from niche config
    3. For each result, check if already discovered (dedup)
    4. Have engager agent evaluate relevance (1-10)
    5. For posts scoring 7+:
       - If has great image → draft as original post (download + credit)
       - If interesting discussion → draft reply
    6. Store everything in discovered_posts + engagement_actions as drafts
    7. Send summary to Telegram for review
    """
```

## jobs/engage.py — Daily Engagement

```python
"""
Posts approved engagement actions throughout the day.
Spaces out actions to avoid spam detection.
"""

async def run_engagement(niche_id: str):
    """
    1. Get approved actions for today
    2. Space them out (minimum 15 min between actions)
    3. Execute: post replies, likes, reposts
    4. Update status in database
    5. Log results
    """
```

## jobs/post.py — Scheduled Posting

```python
"""
Posts approved original content at scheduled times.
"""

async def run_posting(niche_id: str):
    """
    1. Get scheduled posts for current time window
    2. Download images if needed
    3. Upload and post via twikit
    4. Update status
    5. Notify via Telegram
    """
```

## Telegram Review Flow (extend main.py)

New commands:
- `/drafts` — Show today's drafted replies and posts, approve/reject
- `/schedule` — Show upcoming scheduled posts
- `/engage` — Trigger engagement run manually
- `/research` — Trigger research run manually

Notification flow:
1. Research job runs (daily, e.g., 6am)
2. Bot sends you: "Found 8 posts worth engaging with, 3 potential original posts"
3. You tap to review drafts
4. Approve/reject each
5. Engagement job posts approved actions throughout the day

## Cron Schedule

```
06:00  research.py     — Find new content
07:00  Telegram notify  — Send drafts for review
09:00  post.py          — Post first scheduled original
09:30  engage.py        — Start engagement (replies, likes)
13:00  post.py          — Post second scheduled original
14:00  engage.py        — Midday engagement batch
19:00  engage.py        — Evening engagement batch
22:00  metrics.py       — Collect engagement stats, report
```

## Multi-Account Support

Everything is keyed by `niche_id`. To add a second account:

1. Add niche to `config/niches.py` with its own search queries, voice, targets
2. Add X credentials to `.env` (X_USERNAME_brutalist, X_PASSWORD_brutalist, etc)
3. Run the same cron jobs with different niche_id
4. Same Telegram bot, different commands (`/find brutalist`, `/drafts nordic`)

The database already separates by niche column. The agents load per-niche prompts. The voice guide would be per-niche (voice-tatami.md, voice-brutalist.md).

## Build Order

1. **tools/xkit.py** — Get twikit login + search + post working
2. **agents/engager.py** — Draft replies using Claude + voice.md
3. **jobs/research.py** — Search → evaluate → draft pipeline
4. **Database extensions** — New tables
5. **Telegram review flow** — /drafts command with approve/reject
6. **jobs/engage.py** — Post approved actions on schedule
7. **jobs/post.py** — Scheduled original posts
8. **Cron setup** — Wire everything to run daily on Render

## Credentials Needed

For twikit (per account):
- X username
- X email
- X password
- (Session cookies are cached after first login)

No API keys needed. No $200/mo. Just the account credentials.
