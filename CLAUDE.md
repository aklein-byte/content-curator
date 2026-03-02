# Tatami Bot — Claude Code Instructions

Multi-niche social media bot platform. Posts to X/Twitter, Bluesky, and Instagram. Runs on personal VPS (35.245.12.31).

## Stack
- Python 3.11+ with venv (`venv/bin/python`)
- Tweepy (X API v2), atproto (Bluesky), Instagram Graph API
- Anthropic SDK (Haiku for evaluation, Opus for writing/vision)
- SQLite for engagement tracking
- systemd orchestrator on VPS

## Key Files

| File | Purpose |
|---|---|
| `orchestrator.py` | Main loop — reads config, runs scripts on schedule with jitter |
| `post.py` | Draft and post tweets (X + Bluesky) |
| `ig_post.py` | Cross-post to Instagram via Graph API |
| `engage.py` | Like, reply, follow on X |
| `ig_engage.py` | Like, comment, follow on IG (residential proxy) |
| `respond.py` | Reply to mentions and DMs |
| `bookmarks.py` | Fetch bookmarks, draft quote tweets |
| `quote_drafts.py` | Generate quote tweet drafts for dashboard review |
| `real_estate_drafts.py` | Real estate content drafts |
| `thread.py` | Weekly thread generation (disabled) |
| `healthcheck.py` | Daily health check + notifications |
| `audit_followers.py` | Weekly follower audit + unfollow |
| `track_performance.py` | Tweet performance tracking |
| `learn.py` | Analyze top-performing content for patterns |
| `museum_fetch.py` | Fetch museum objects for @MuseumStories |
| `dashboard.py` | Flask dashboard for post review/approval |

## Niche Configs

Each niche has its own config file. The orchestrator reads `--config` arg.

| Config | Niche | Account |
|---|---|---|
| `config.json` | @tatamispaces | Japanese architecture/interior |
| `config-museum.json` | @MuseumStories | Museum objects and history |

Config structure: scripts (schedule, limits, timeouts), models, delays, IG hashtags, posting window.

## Dev Commands

```bash
# Dry-run a post (no actual posting)
venv/bin/python post.py --dry-run

# Post a specific draft by ID
venv/bin/python post.py --post-id 42

# Run healthcheck
venv/bin/python healthcheck.py

# Dashboard locally
venv/bin/python dashboard.py
# → http://localhost:8080

# Museum: fetch new objects
venv/bin/python museum_fetch.py --max-items 20

# Museum: bypass scheduler, post specific ID
venv/bin/python post.py --config config-museum.json --post-id 123
```

## VPS Operations

```bash
ssh growbot

# Watch orchestrator logs
sudo journalctl -u tatami-orchestrator -f

# Check timer schedule
systemctl list-timers tatami*

# Dashboard status
sudo systemctl status tatami-dashboard

# Restart after deploy
sudo systemctl restart tatami-orchestrator.timer
sudo systemctl restart tatami-dashboard

# Telegram bot
sudo systemctl status tatami-telegram-bot
```

## Deploy

1. Push to GitHub (`aklein-byte/content-curator`)
2. SSH to VPS: `ssh growbot`
3. Pull: `cd ~/tatami-bot && git pull`
4. Restart services if needed: `sudo systemctl restart tatami-orchestrator.timer`

## Data Files (VPS is source of truth)

- `posts.json` — tatamispaces posts (status, votes, tweet IDs)
- `posts-museumstories.json` — museum posts
- `ig-engagement-log.json` — IG engagement history

**Never overwrite VPS JSON files with local copies.** The dashboard modifies these files (approve/reject/vote). Use the dashboard API for edits.

## Knowledge Vault

Brain vault notes for this project:
- `areas/tatami-bot/vps-config.md` — VPS services, schedules, niches
- `areas/tatami-bot/ig-engage.md` — IG engagement: proxy, cookies, rate limits
- `areas/museum-stories/post-feedback.md` — writing rules, image rules
- `reference/x-engagement-research.md` — X algorithm, posting strategy
- `reference/diagrams/vps-infrastructure.md` — infrastructure diagram

## Auto-Learnings
<!-- Auto-generated from session analysis. -->


- [2026-03-02] Instagram engagement requires periodic cookie/login refresh across accounts — "you need to switch login for cookies", "run it for both accounts now we havent done it in a long time."
- [2026-03-02] All discovered rate limits and API behaviors must be documented in the Obsidian vault, not just tested.
- [2026-03-02] Museum draft generation should run on the personal VPS (growbot machine), not locally.
