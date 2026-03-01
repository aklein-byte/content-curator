# tatami-bot — @TatamiSpaces & @MuseumStories Automation

Autonomous content curation and social media automation for two X/Twitter accounts:
- **[@TatamiSpaces](https://x.com/tatamispaces)** — Japanese architecture & interior design
- **[@MuseumStories](https://x.com/museumstories)** — Stories behind museum objects

## Architecture Overview

```
orchestrator.py (systemd timer, every 5 min)
  ├── post.py            → Posts to X (via official API), cross-posts to IG + Communities
  ├── ig_post.py         → Catches up IG cross-posts that post.py missed
  ├── engage.py          → Likes, replies, follows in niche (X API) + saves observations
  ├── ig_engage.py       → IG engagement via Playwright headless browser
  ├── respond.py         → Replies to mentions/replies on our posts
  ├── bookmarks.py       → Fetches X bookmarks, creates draft posts
  ├── quote_drafts.py    → Finds quotable tweets, drafts QT commentary via Claude
  ├── real_estate_drafts.py → Scrapes JP real estate sites, creates listing drafts
  ├── museum_fetch.py    → Fetches museum objects from Met/Cleveland/SMK/Harvard APIs
  ├── healthcheck.py     → Verifies system health
  ├── audit_followers.py → Weekly follower audit + unfollows
  ├── track_performance.py → Measures own post metrics via X API
  └── learn.py           → Daily AI analysis → insights for prompt improvement

telegram_bot.py → Conversational AI approval bot (systemd service)
dashboard.py    → Web UI on port 8080 (proxied via nginx at /dashboard/)
```

## Multi-Account Setup

Two orchestrators run independently:
- `config.json` → @TatamiSpaces (tatamispaces niche)
- `config-museum.json` → @MuseumStories (museumstories niche)

Each has its own posts file, schedule, and model config. Niche configs in `config/niches.py`.

## Key Files

| File | Purpose |
|------|---------|
| `config.json` | Tatami orchestrator schedule, model config, posting windows, limits |
| `config-museum.json` | Museum orchestrator schedule and config |
| `posts.json` | Tatami posts (draft → approved → posted). Source of truth. |
| `posts-museumstories.json` | Museum posts. Same lifecycle. |
| `engagement-log.json` | X engagement action history |
| `config/niches.py` | Niche config: search queries, communities, hashtags, prompts, engage limits, museum APIs |
| `config/voice.md` | Writing style guide (critical — read before touching captions) |
| `agents/engager.py` | Reply drafting, post evaluation, original post prompts |
| `agents/writer.py` | Caption writing with Claude Opus |
| `agents/fact_checker.py` | Fact extraction, source verification, QA validation for museum posts |
| `tools/xapi.py` | X API v2 wrapper (OAuth 1.0a + OAuth 2.0 PKCE for bookmarks) |
| `tools/common.py` | Shared utilities: load_json, save_json, notify, get_model, parse_json_response |
| `tools/post_queue.py` | Queue management: load/save posts, schedule slots, dedup, ID generation |
| `tools/humanizer.py` | Anti-AI writing validator (banned words, phrases, regex patterns) |
| `tools/museum_apis.py` | Museum API clients (Met, Cleveland, SMK, Harvard) |
| `tests/test_syntax.py` | Syntax, import, config, and model role verification |

## Model Configuration

All Claude model IDs are centralized via `get_model(role)` in `tools/common.py`. No hardcoded model strings in scripts.

| Role | Default | Used by |
|------|---------|---------|
| `writer` | claude-opus-4-6 | Caption generation, museum story writing |
| `rewrite` | claude-opus-4-6 | Fact-check rewrites, caption regeneration |
| `vision` | claude-opus-4-6 | Image description for threads |
| `reply_drafter` | claude-opus-4-6 | Engagement replies, IG comments |
| `evaluator` | claude-haiku-4-5 | Post relevance scoring, follower audit |
| `scorer` | claude-haiku-4-5 | Museum story potential, learning analysis |
| `fact_extract` | claude-haiku-4-5 | Claim extraction from drafts |
| `fact_research` | claude-haiku-4-5 | Web-assisted claim verification |
| `fact_qa` | claude-haiku-4-5 | Final draft QA validation |
| `chat` | claude-sonnet-4-6 | Telegram bot conversation |
| `quote_writer` | claude-sonnet-4-6 | Quote tweet commentary |
| `enrich_vision` | claude-sonnet-4-6 | Bookmark context enrichment |

Override any role in `config.json` / `config-museum.json` under `"models": {}`.

## Engagement Limits

Per-niche limits live in `config/niches.py` under `engage_limits`:

```python
"engage_limits": {
    "daily_max_replies": 30,
    "daily_max_likes": 60,
    "daily_max_follows": 10,
    "min_author_followers_for_reply": 300,
    "min_post_likes_for_reply": 3,
    "like_delay": [10, 35],    # seconds (min, max)
    "reply_delay": [30, 120],
    "follow_delay": [20, 60],
}
```

Posting limits live in config JSON under `"posting_window"`:
- `max_per_day`, `min_gap_hours`, `min_post_score`, `low_queue_threshold`, `min_image_dimension`

## Testing & Pre-commit Hook

**Pre-commit hook** (`.git/hooks/pre-commit`):
- Runs `py_compile` on all staged `.py` files
- Runs `tests/test_syntax.py` full suite

**Test suite** (`tests/test_syntax.py`):
- Compiles all `.py` files for syntax errors
- Imports key modules to catch broken dependencies
- Validates niche configs have required keys and engage_limits
- Verifies all 12 model roles resolve to valid `claude-*` IDs

```bash
# Run tests manually
sudo -u amit venv/bin/python tests/test_syntax.py
```

## Telegram Bot (@Tatami_Curator_Bot)

Conversational AI assistant for content review and management. Runs as `tatami-telegram-bot.service`.

**Commands:**
- `/q` — Review tatami drafts
- `/qm` — Review museum drafts
- `/stats` — Queue counts
- `/insights` — View learned insights from data
- `/experiments` — View/approve content experiments
- `/learn` — Trigger learning engine manually

**Conversational features (Claude Sonnet powered):**
- Natural language approve/drop/skip ("ok", "drop it", "skip")
- Image selection ("drop the first pic", "use pics 2-4")
- Jump to any post ("show me 51", "go back to post 51")
- Undo actions ("undo 51", "unapprove 51")
- Ask questions about posts ("does this have a price?")
- Teach preferences ("always include prices", "avoid blurry photos")

**Key files:**
- `telegram_bot.py` — Main bot code
- `data/bot-memory.json` — Persistent preferences
- `data/bot-decisions.json` — Decision log (approve/drop with context)

**Config:** `.env` has `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`.

## Learning Engine

Self-improving system that observes what works and feeds insights back into prompts.

### Data Flow
```
engage.py → data/observations-{niche}.json    (high-performing tweets in niche)
telegram_bot.py → data/bot-decisions.json      (owner approve/drop patterns)
track_performance.py → posts.json[performance]  (our own post metrics)
         ↓
learn.py → data/insights-{niche}.json          (analyzed patterns via Sonnet)
         ↓
agents/engager.py (reply/post prompts)         (insights injected automatically)
agents/writer.py (caption prompts)             (insights injected automatically)
```

### Key files
| File | Purpose | Schedule |
|------|---------|----------|
| `track_performance.py` | Measures own tweets' likes/views/reposts via X API | 2x/day (6am, 6pm ET) |
| `learn.py` | Analyzes all data with Sonnet, produces insights | Daily 5am ET |
| `data/insights-{niche}.json` | Structured insights for prompt injection | Auto-generated |
| `data/observations-{niche}.json` | High-performing tweets observed during engagement | Auto-captured |
| `data/experiments.json` | Content experiment tracking | Managed via Telegram |

### How insights reach prompts
- `agents/engager.py` has `_load_insights(niche_id)` — injected into reply and original post prompts
- `agents/writer.py` has `_load_writing_insights(niche_id)` — injected into caption prompts
- Insights auto-expire after 7 days (stale data won't pollute prompts)

### Experiments
- `learn.py` proposes experiments (new content types to try)
- Owner approves/rejects via Telegram `/experiments` command
- Active experiments tracked in `data/experiments.json`

## Museum Pipeline (museumstories)

```
museum_fetch.py
  1. Fetch candidates from APIs (Met, Cleveland, SMK, Harvard)
  2. NIMA/TOPIQ aesthetic scoring gate (min 5.0/5.2)
  3. Category/museum weighting (Cleveland +5, Met +3, prints/jewelry 0.3x penalty)
  4. Haiku scores story potential (1-10)
  5. Novelty check against all posted object IDs
  6. Opus writes full story (single tweet or multi-tweet thread)
  7. fact_checker.py: extract claims → verify vs source → web research → rewrite
  8. humanizer.py: banned words/phrases/regex patterns
  9. quick_validate(): Haiku one-pass QA sanity check
  10. Add to posts-museumstories.json as approved + scheduled
```

**Thread format rules:**
- Threads require 2+ images (single-image threads look awkward)
- Each tweet's text must reference its attached image
- Image 1 = full object, subsequent = details/alternate views
- Cleveland API has `alternate_images` field with multiple views

**Scoring weights:** `(meta * 0.35 + image * 0.45 + novelty * 0.2 + museum_bonus) * category_penalty`

## Fact-Checking Pipeline (agents/fact_checker.py)

Three-stage validation for museum posts:
1. **Extract claims** (Haiku): Pull specific names, dates, numbers, materials from draft
2. **Verify claims** (Haiku): Cross-reference against source tweet text + enriched context
3. **Rewrite if needed** (Opus): Fix inaccurate claims while preserving voice

Plus a final **quick_validate** pass (Haiku): One-shot sanity check that allows approximate ages from birth/death years, well-known period facts, and reasonable inferences.

`SourceContext` dataclass carries the original source material through the pipeline.

## API & Auth

- **X API v2** (Official): OAuth 1.0a via `tools/xapi.py`. Credentials in `.env`.
- **Instagram**: Playwright headless browser with persistent profile in `data/ig_browser_profile/`.
- **Anthropic Claude**: Opus for writing, Sonnet for chat/analysis, Haiku for evaluation. Key in `.env`.
- **Telegram Bot API**: Long-polling via python-telegram-bot. Token in `.env`.

## Post Lifecycle

1. **Source**: Bookmarks, quote tweet search, real estate scraper, or museum API fetch
2. **Draft**: Added to posts file with `status: "draft"`
3. **Review**: Owner reviews via Telegram bot (or dashboard)
4. **Approve**: `status: "approved"`, `scheduled_for` set
5. **Post**: post.py picks best approved post → publishes → `status: "posted"`
6. **Track**: track_performance.py measures engagement metrics

### Image Selection (tatami posts)
- `image_indices: [1,2,3]` — use only these images (0-indexed)
- `image_index: 0` — use only this one image
- `image_count: 2` — use first N images

### Post Types
- `repost-with-credit`: Image post crediting original source
- `quote-tweet`: Quote tweet with commentary
- `real-estate`: Japanese property listing with price/details
- `bookmark`: Repost from bookmarks
- `original`: Original content

## Notifications

Telegram first (via bot API), ntfy.sh fallback. Configured in `tools/common.py` `notify()`.

## Infrastructure

- **VPS**: GCP `dev-server` in `us-east4-c` (e2-medium, Ubuntu 24.04)
- **User**: `amit` (owns all files). SSH as `aklein_watchungpediatrics_com`.
- **Systemd services:**
  - `tatami-orchestrator.timer` — runs orchestrator every 5 min
  - `tatami-telegram-bot.service` — Telegram bot (always running)
- **Xvfb**: Virtual display :99 for Playwright/headless Chrome (IG)
- **Python**: `venv/bin/python` (venv in project root)

## Common Operations

```bash
# Check orchestrator status
sudo -u amit /home/amit/tatami-bot/venv/bin/python orchestrator.py status

# Manual post (dry run)
sudo -u amit /home/amit/tatami-bot/venv/bin/python post.py --dry-run --niche tatamispaces

# Watch orchestrator logs
sudo journalctl -u tatami-orchestrator -f

# Watch telegram bot logs
sudo journalctl -u tatami-telegram-bot -f

# Restart bot after changes
sudo systemctl restart tatami-telegram-bot

# Run learning engine manually
sudo -u amit /home/amit/tatami-bot/venv/bin/python learn.py --dry-run

# Track performance manually
sudo -u amit /home/amit/tatami-bot/venv/bin/python track_performance.py
```

## Shared Utilities (tools/common.py)

| Function | Purpose |
|----------|---------|
| `load_json(path, default)` | Safe JSON load with fallback |
| `save_json(path, data)` | Atomic JSON write |
| `get_model(role)` | Resolve model ID from config with defaults |
| `get_anthropic()` | Cached Anthropic client singleton |
| `parse_json_response(text)` | Extract JSON from Claude response text |
| `load_voice_guide(niche_id)` | Load niche-specific voice guide with caching |
| `load_config()` | Load active config JSON |
| `setup_logging(name)` | Configure logger with consistent format |
| `notify(title, message)` | Send notification (Telegram + ntfy.sh fallback) |
| `acquire_lock(path)` / `release_lock(fd)` | File-based process locking |
| `random_delay(label, min, max)` | Async sleep for rate limiting |
| `niche_log_path(filename, niche_id)` | Resolve niche-specific log file path |

## Adding a New Niche

1. Add niche config to `config/niches.py` with required keys: `handle`, `description`, `engagement`, `engage_limits`
2. Create orchestrator config: `config-{name}.json` (copy from existing)
3. Add X API credentials to `.env` and map in `x_api_env`
4. Create voice guide: `config/voice-{niche_id}.md` (optional, falls back to `voice.md`)
5. Create systemd timer (copy `tatami-orchestrator.timer`)
6. Run `tests/test_syntax.py` to validate config

## Important Notes

- **File permissions**: Files owned by `amit`. Use `sudo -u amit` for all operations.
- **Voice guide**: `config/voice.md` is the bible for all written content. Read it before changing any caption logic.
- **No auto-publish for drafts**: Draft posts require manual approval. Only `approved` posts get published.
- **Museum image sources**: Met, Cleveland, SMK, Harvard. AIC removed (Cloudflare blocking).
- **X free tier**: Returns 0 impression data; metrics only show likes/replies/reposts/bookmarks.
- **IG headless**: Uses persistent browser profile. If login expires, use VNC (`~/start-vnc.sh`) to re-auth.

## Knowledge Vault
Brain vault at `~/brain/` contains project context, decisions, reference docs, and people notes. Key paths:
- `areas/tatami-bot/` — engagement analysis, VPS config, performance data
- `areas/museum-stories/` — post feedback, writing rules, launch log
- `reference/x-engagement-research.md` — X/Twitter algorithm weights, posting times, rate limits
- `decisions/` — architecture choices, platform evaluations
- `people/` — key contacts and roles
- `runbooks/` — operational procedures

Check the vault before asking the user for context that may already be documented.
