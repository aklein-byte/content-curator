"""
SQLite database layer for tatami-bot.

Replaces JSON files + fcntl locks with a single WAL-mode SQLite database.
All niches share one DB with niche_id columns. Concurrent readers don't
block writers.

Usage:
    from tools.db import get_db, init_db, acquire_process_lock, release_process_lock
"""

import os
import json
import sqlite3
import logging
from pathlib import Path
from datetime import datetime, timezone

log = logging.getLogger("db")

BASE_DIR = Path(__file__).parent.parent
DEFAULT_DB_PATH = BASE_DIR / "data" / "tatami.db"

_connection: sqlite3.Connection | None = None


def get_db(readonly: bool = False) -> sqlite3.Connection:
    """Get or create singleton DB connection.

    WAL mode + busy_timeout=10s for concurrent access.
    Returns rows as sqlite3.Row (dict-like access).
    """
    global _connection

    db_path = Path(os.environ.get("TATAMI_DB", str(DEFAULT_DB_PATH)))
    db_path.parent.mkdir(parents=True, exist_ok=True)

    if _connection is not None:
        try:
            _connection.execute("SELECT 1")
            return _connection
        except sqlite3.ProgrammingError:
            _connection = None

    uri = f"file:{db_path}"
    if readonly:
        uri += "?mode=ro"

    conn = sqlite3.connect(
        uri if readonly else str(db_path),
        uri=readonly,
        timeout=10.0,
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute("PRAGMA foreign_keys=ON")

    if not readonly:
        _connection = conn

    return conn


def close_db():
    """Close the singleton connection."""
    global _connection
    if _connection is not None:
        _connection.close()
        _connection = None


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

SCHEMA_SQL = """
-- Posts: replaces posts.json / posts-museumstories.json / posts-cosmicshots.json
-- id is niche-scoped (both niches start at 1), so PK is (niche_id, id)
CREATE TABLE IF NOT EXISTS posts (
    niche_id        TEXT NOT NULL,
    id              INTEGER NOT NULL,
    type            TEXT,                       -- original, repost-with-credit, quote, quote-tweet, museum
    status          TEXT NOT NULL DEFAULT 'draft',
    text            TEXT,
    source_url      TEXT,
    source_handle   TEXT,
    source          TEXT,                       -- engage, bookmarks, etc.
    score           INTEGER,
    scheduled_for   TEXT,
    posted_at       TEXT,
    created_at      TEXT,
    category        TEXT,

    -- X/Twitter
    tweet_id        TEXT,
    thread_tweet_ids TEXT,                      -- JSON array of strings
    thread_captions TEXT,                       -- JSON array of strings
    community_tweet_id TEXT,
    community_id    TEXT,
    community_posts TEXT,                       -- JSON array of {community_id, tweet_id}

    -- Images
    image           TEXT,                       -- local relative path
    image_urls      TEXT,                       -- JSON array of URLs
    image_index     INTEGER,
    image_count     INTEGER,

    -- Instagram
    ig_posted       INTEGER DEFAULT 0,         -- boolean
    ig_posted_at    TEXT,
    ig_media_id     TEXT,
    ig_container_created INTEGER DEFAULT 0,
    ig_container_created_at TEXT,
    ig_publish_error TEXT,
    ig_images_attempted INTEGER,
    ig_skip_reason  TEXT,
    ig_carousel     INTEGER,                   -- boolean

    -- Quote tweet fields
    quote_tweet_id  TEXT,
    quote_author    TEXT,
    quote_text      TEXT,
    quote_likes     INTEGER,

    -- Status tracking
    skip_reason     TEXT,
    fail_reason     TEXT,
    posting_started TEXT,
    dedup_recovered INTEGER DEFAULT 0,         -- boolean
    pinned          INTEGER DEFAULT 0,         -- boolean
    posted          INTEGER DEFAULT 0,         -- legacy boolean

    -- Notes / dashboard
    notes           TEXT,
    vote            TEXT,
    _previous_text  TEXT,

    -- Museum-specific inline (simple fields)
    object_id       TEXT,
    museum          TEXT,
    title           TEXT,
    artist          TEXT,
    date            TEXT,
    medium          TEXT,
    culture         TEXT,
    period          TEXT,
    thread          INTEGER DEFAULT 0,         -- boolean: is this a thread post?
    object_url      TEXT,
    generated_at    TEXT,
    format          TEXT,                       -- older posts: "thread" or "single"
    dimensions      TEXT,
    allImages       TEXT,                       -- JSON array of all image URLs
    PRIMARY KEY (niche_id, id)
);

CREATE INDEX IF NOT EXISTS idx_posts_niche_status ON posts(niche_id, status);
CREATE INDEX IF NOT EXISTS idx_posts_niche_scheduled ON posts(niche_id, scheduled_for);
CREATE INDEX IF NOT EXISTS idx_posts_source_url ON posts(source_url);
CREATE INDEX IF NOT EXISTS idx_posts_tweet_id ON posts(tweet_id);

-- Museum tweets: 1:N thread components for museum posts
CREATE TABLE IF NOT EXISTS museum_tweets (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    niche_id    TEXT NOT NULL,
    post_id     INTEGER NOT NULL,
    tweet_index INTEGER NOT NULL,
    text        TEXT,
    image_url   TEXT,
    images      TEXT,                           -- JSON array of int indices
    _previous_text TEXT,
    UNIQUE(niche_id, post_id, tweet_index)
);

CREATE INDEX IF NOT EXISTS idx_museum_tweets_post ON museum_tweets(niche_id, post_id);

-- Engagement log: replaces engagement-log*.json and ig-engagement-log*.json
CREATE TABLE IF NOT EXISTS engagement_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    niche_id        TEXT NOT NULL,
    platform        TEXT NOT NULL,              -- x, ig, bluesky
    action          TEXT NOT NULL,              -- like, reply, comment, follow, respond
    post_id         TEXT,                       -- tweet ID or IG shortcode
    author          TEXT,
    author_handle   TEXT,
    score           INTEGER,
    post_likes      INTEGER,
    author_followers INTEGER,
    query           TEXT,
    reason          TEXT,
    timestamp       TEXT NOT NULL,

    -- Reply-specific
    reply_id        TEXT,
    reply_text      TEXT,
    comment         TEXT,                       -- IG comment text
    shortcode       TEXT,                       -- IG shortcode

    -- Reply performance tracking
    reply_likes     INTEGER,
    reply_replies   INTEGER,
    reply_retweets  INTEGER,
    checked_at      TEXT,

    -- Respond-specific
    reply_to_tweet_id TEXT,
    our_response_id TEXT,
    replier         TEXT,
    our_response    TEXT,
    parent_id       TEXT
);

CREATE INDEX IF NOT EXISTS idx_engagement_niche_platform ON engagement_log(niche_id, platform, action, timestamp);
CREATE INDEX IF NOT EXISTS idx_engagement_post ON engagement_log(post_id);
CREATE INDEX IF NOT EXISTS idx_engagement_shortcode ON engagement_log(shortcode);

-- Orchestrator status: replaces orchestrator-status*.json
CREATE TABLE IF NOT EXISTS orchestrator_status (
    config_name     TEXT NOT NULL,
    script_name     TEXT NOT NULL,
    last_run        TEXT,
    last_status     TEXT,
    last_exit_code  INTEGER,
    last_duration   REAL,
    last_metrics    TEXT,                       -- JSON
    last_error      TEXT,
    runs_today      TEXT,                       -- JSON: {"YYYY-MM-DD": count}
    slots_done      TEXT,                       -- JSON array of "YYYY-MM-DD_N"
    consecutive_failures INTEGER DEFAULT 0,
    running         INTEGER DEFAULT 0,
    PRIMARY KEY (config_name, script_name)
);

-- Orchestrator jitter: daily jitter offsets
CREATE TABLE IF NOT EXISTS orchestrator_jitter (
    config_name     TEXT NOT NULL,
    jitter_date     TEXT NOT NULL,
    daily_jitter    TEXT NOT NULL,              -- JSON: {script: [offsets]}
    PRIMARY KEY (config_name, jitter_date)
);

-- Process locks: replaces .lock files
CREATE TABLE IF NOT EXISTS process_locks (
    lock_name       TEXT PRIMARY KEY,
    pid             INTEGER NOT NULL,
    acquired_at     TEXT NOT NULL,
    heartbeat_at    TEXT NOT NULL
);

-- IG post log: replaces ig-post-log*.json
CREATE TABLE IF NOT EXISTS ig_post_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    niche_id    TEXT NOT NULL,
    post_id     INTEGER NOT NULL,
    ig_media_id TEXT,
    error       TEXT,
    timestamp   TEXT NOT NULL
);

-- Insights: replaces data/insights-*.json
CREATE TABLE IF NOT EXISTS insights (
    niche_id    TEXT PRIMARY KEY,
    data        TEXT NOT NULL,                  -- full JSON blob
    updated_at  TEXT NOT NULL
);

-- Respond since ID: replaces data/respond-since-id.txt
CREATE TABLE IF NOT EXISTS kv_store (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL
);
"""


_db_initialized = False


def init_db():
    """Create all tables if they don't exist. No-op after first call."""
    global _db_initialized
    if _db_initialized:
        return
    db = get_db()
    db.executescript(SCHEMA_SQL)
    db.commit()
    _db_initialized = True


# ---------------------------------------------------------------------------
# JSON column helpers
# ---------------------------------------------------------------------------

def json_dumps(obj) -> str | None:
    """Serialize to JSON string, or None if obj is None/empty."""
    if obj is None:
        return None
    if isinstance(obj, (list, dict)):
        if not obj:
            return None
        return json.dumps(obj, ensure_ascii=False, default=str)
    return str(obj)


def json_loads(s: str | None, default=None):
    """Parse JSON string, returning default on None/error."""
    if s is None:
        return default
    try:
        return json.loads(s)
    except (json.JSONDecodeError, TypeError):
        return default


# ---------------------------------------------------------------------------
# Process locks (replaces fcntl file locks)
# ---------------------------------------------------------------------------

def acquire_process_lock(name: str) -> bool:
    """Try to acquire a named process lock.

    Returns True if acquired, False if another live process holds it.
    Auto-reclaims locks from dead processes (PID check) or stale heartbeats (>5 min).
    Uses BEGIN IMMEDIATE to prevent TOCTOU races between concurrent processes.
    """
    db = get_db()
    pid = os.getpid()
    now = datetime.now(timezone.utc).isoformat()

    # BEGIN IMMEDIATE acquires a write lock upfront, preventing races
    db.execute("BEGIN IMMEDIATE")
    try:
        row = db.execute(
            "SELECT pid, heartbeat_at FROM process_locks WHERE lock_name = ?",
            (name,)
        ).fetchone()

        if row:
            existing_pid = row["pid"]
            heartbeat = row["heartbeat_at"]

            # Check if the holding process is still alive
            if existing_pid != pid:
                try:
                    os.kill(existing_pid, 0)  # signal 0 = check existence
                    # Process alive — check heartbeat staleness
                    if heartbeat:
                        hb_time = datetime.fromisoformat(heartbeat)
                        age_seconds = (datetime.now(timezone.utc) - hb_time).total_seconds()
                        if age_seconds < 300:  # 5 minutes
                            db.execute("ROLLBACK")
                            return False  # Lock is held and fresh
                        else:
                            log.warning(
                                f"Reclaiming stale lock '{name}' from PID {existing_pid} "
                                f"(heartbeat {age_seconds:.0f}s old)"
                            )
                    else:
                        db.execute("ROLLBACK")
                        return False
                except ProcessLookupError:
                    log.warning(f"Reclaiming lock '{name}' from dead PID {existing_pid}")
                except PermissionError:
                    # Process exists but we can't signal it — assume alive
                    db.execute("ROLLBACK")
                    return False

            # Reclaim or re-acquire
            db.execute(
                "UPDATE process_locks SET pid = ?, acquired_at = ?, heartbeat_at = ? WHERE lock_name = ?",
                (pid, now, now, name),
            )
        else:
            db.execute(
                "INSERT INTO process_locks (lock_name, pid, acquired_at, heartbeat_at) VALUES (?, ?, ?, ?)",
                (name, pid, now, now),
            )

        db.execute("COMMIT")
        return True
    except Exception:
        db.execute("ROLLBACK")
        raise


def release_process_lock(name: str):
    """Release a named process lock."""
    db = get_db()
    db.execute("DELETE FROM process_locks WHERE lock_name = ? AND pid = ?", (name, os.getpid()))
    db.commit()


def heartbeat_lock(name: str):
    """Update heartbeat timestamp for a held lock."""
    db = get_db()
    now = datetime.now(timezone.utc).isoformat()
    db.execute(
        "UPDATE process_locks SET heartbeat_at = ? WHERE lock_name = ? AND pid = ?",
        (now, name, os.getpid()),
    )
    db.commit()


# ---------------------------------------------------------------------------
# Post helpers
# ---------------------------------------------------------------------------

# Columns that store JSON arrays/objects (need serialize/deserialize)
_JSON_COLUMNS = {
    "image_urls", "thread_tweet_ids", "thread_captions",
    "community_posts", "allImages",
}

# Boolean columns stored as INTEGER in SQLite
_BOOL_COLUMNS = {
    "ig_posted", "ig_container_created", "ig_carousel",
    "dedup_recovered", "pinned", "posted", "thread",
}


def _post_row_to_dict(row: sqlite3.Row) -> dict:
    """Convert a posts table row to the dict format scripts expect."""
    d = dict(row)

    # Deserialize JSON columns
    for col in _JSON_COLUMNS:
        if col in d and d[col] is not None:
            d[col] = json_loads(d[col], default=[])
        elif col in d:
            if col == "image_urls":
                d[col] = []

    # Convert INTEGER booleans back to Python bools
    for col in _BOOL_COLUMNS:
        if col in d and d[col] is not None:
            d[col] = bool(d[col])

    # Remove None values to match original JSON behavior (keys absent = not set)
    return {k: v for k, v in d.items() if v is not None}


def _post_dict_to_params(post: dict, niche_id: str) -> dict:
    """Convert a post dict to DB column params for INSERT/UPDATE."""
    params = {"niche_id": niche_id}

    for key, val in post.items():
        if key == "tweets":
            continue  # handled separately in museum_tweets table
        if key in _JSON_COLUMNS:
            params[key] = json_dumps(val)
        elif key in _BOOL_COLUMNS:
            params[key] = int(val) if val is not None else None
        else:
            params[key] = val

    return params


def get_post(niche_id: str, post_id: int) -> dict | None:
    """Fetch a single post by ID."""
    db = get_db()
    row = db.execute(
        "SELECT * FROM posts WHERE id = ? AND niche_id = ?",
        (post_id, niche_id),
    ).fetchone()
    if not row:
        return None

    post = _post_row_to_dict(row)

    # Attach museum tweets if this is a museum post
    if post.get("type") == "museum":
        tweets = db.execute(
            "SELECT * FROM museum_tweets WHERE niche_id = ? AND post_id = ? ORDER BY tweet_index",
            (niche_id, post_id),
        ).fetchall()
        if tweets:
            post["tweets"] = []
            for tw in tweets:
                td = dict(tw)
                td.pop("id", None)
                td.pop("post_id", None)
                if td.get("images"):
                    td["images"] = json_loads(td["images"], default=[])
                # Remove None values
                td = {k: v for k, v in td.items() if v is not None}
                post["tweets"].append(td)

    return post


def get_all_posts(niche_id: str) -> list[dict]:
    """Fetch all posts for a niche, with museum tweets attached."""
    db = get_db()
    rows = db.execute(
        "SELECT * FROM posts WHERE niche_id = ? ORDER BY id",
        (niche_id,),
    ).fetchall()

    posts = [_post_row_to_dict(r) for r in rows]

    # Batch-load museum tweets for museum posts
    museum_ids = [p["id"] for p in posts if p.get("type") == "museum"]
    if museum_ids:
        placeholders = ",".join("?" * len(museum_ids))
        tweet_rows = db.execute(
            f"SELECT * FROM museum_tweets WHERE niche_id = ? AND post_id IN ({placeholders}) ORDER BY post_id, tweet_index",
            [niche_id] + museum_ids,
        ).fetchall()

        # Group by post_id
        tweets_by_post: dict[int, list] = {}
        for tw in tweet_rows:
            pid = tw["post_id"]
            td = dict(tw)
            td.pop("id", None)
            td.pop("post_id", None)
            if td.get("images"):
                td["images"] = json_loads(td["images"], default=[])
            td = {k: v for k, v in td.items() if v is not None}
            tweets_by_post.setdefault(pid, []).append(td)

        for post in posts:
            if post.get("type") == "museum" and post["id"] in tweets_by_post:
                post["tweets"] = tweets_by_post[post["id"]]

    return posts


def insert_post(niche_id: str, post_dict: dict, _commit: bool = True) -> int:
    """Insert a new post and return its ID.

    Set _commit=False when batching multiple operations (caller commits).
    """
    db = get_db()
    params = _post_dict_to_params(post_dict, niche_id)

    # If the post has an explicit ID, use it; otherwise auto-assign
    if "id" in params:
        cols = list(params.keys())
        placeholders = ", ".join("?" * len(cols))
        col_names = ", ".join(cols)
        db.execute(
            f"INSERT OR REPLACE INTO posts ({col_names}) VALUES ({placeholders})",
            [params[c] for c in cols],
        )
        post_id = params["id"]
    else:
        # Auto-increment: find max ID for this niche + 1
        row = db.execute(
            "SELECT COALESCE(MAX(id), 0) + 1 AS next_id FROM posts WHERE niche_id = ?",
            (niche_id,),
        ).fetchone()
        post_id = row["next_id"]
        params["id"] = post_id
        cols = list(params.keys())
        placeholders = ", ".join("?" * len(cols))
        col_names = ", ".join(cols)
        db.execute(
            f"INSERT INTO posts ({col_names}) VALUES ({placeholders})",
            [params[c] for c in cols],
        )

    # Insert museum tweets if present
    tweets = post_dict.get("tweets", [])
    if tweets:
        for idx, tw in enumerate(tweets):
            db.execute(
                """INSERT OR REPLACE INTO museum_tweets
                   (niche_id, post_id, tweet_index, text, image_url, images, _previous_text)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    niche_id, post_id, idx,
                    tw.get("text"),
                    tw.get("image_url"),
                    json_dumps(tw.get("images")),
                    tw.get("_previous_text"),
                ),
            )

    if _commit:
        db.commit()
    return post_id


def update_post(niche_id: str, post_id: int, _commit: bool = True, **fields):
    """Update specific fields on a single post.

    Set _commit=False when batching multiple operations (caller commits).
    """
    if not fields:
        return

    db = get_db()
    sets = []
    values = []

    for key, val in fields.items():
        if key == "tweets":
            # Update museum tweets separately
            for idx, tw in enumerate(val):
                db.execute(
                    """INSERT OR REPLACE INTO museum_tweets
                       (niche_id, post_id, tweet_index, text, image_url, images, _previous_text)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (
                        niche_id, post_id, idx,
                        tw.get("text"),
                        tw.get("image_url"),
                        json_dumps(tw.get("images")),
                        tw.get("_previous_text"),
                    ),
                )
            continue

        if key in _JSON_COLUMNS:
            val = json_dumps(val)
        elif key in _BOOL_COLUMNS:
            val = int(val) if val is not None else None

        sets.append(f"{key} = ?")
        values.append(val)

    if sets:
        values.extend([post_id, niche_id])
        db.execute(
            f"UPDATE posts SET {', '.join(sets)} WHERE id = ? AND niche_id = ?",
            values,
        )

    if _commit:
        db.commit()


# ---------------------------------------------------------------------------
# Engagement log helpers
# ---------------------------------------------------------------------------

def log_engagement(niche_id: str, platform: str, action: str, **kwargs):
    """Insert an engagement log entry."""
    db = get_db()
    db.execute(
        """INSERT INTO engagement_log
           (niche_id, platform, action, post_id, author, author_handle,
            score, post_likes, author_followers, query, reason, timestamp,
            reply_id, reply_text, comment, shortcode,
            reply_to_tweet_id, our_response_id, replier, our_response, parent_id)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            niche_id, platform, action,
            kwargs.get("post_id"),
            kwargs.get("author"),
            kwargs.get("author_handle"),
            kwargs.get("score"),
            kwargs.get("post_likes"),
            kwargs.get("author_followers"),
            kwargs.get("query"),
            kwargs.get("reason"),
            kwargs.get("timestamp", datetime.now(timezone.utc).isoformat()),
            kwargs.get("reply_id"),
            kwargs.get("reply_text"),
            kwargs.get("comment"),
            kwargs.get("shortcode"),
            kwargs.get("reply_to_tweet_id"),
            kwargs.get("our_response_id"),
            kwargs.get("replier"),
            kwargs.get("our_response"),
            kwargs.get("parent_id"),
        ),
    )
    db.commit()


def already_engaged(niche_id: str, platform: str, action: str, post_id: str) -> bool:
    """Check if we already took an action on a post."""
    db = get_db()
    if platform == "ig":
        row = db.execute(
            "SELECT 1 FROM engagement_log WHERE niche_id = ? AND platform = ? AND action = ? AND shortcode = ? LIMIT 1",
            (niche_id, platform, action, post_id),
        ).fetchone()
    else:
        row = db.execute(
            "SELECT 1 FROM engagement_log WHERE niche_id = ? AND platform = ? AND action = ? AND post_id = ? LIMIT 1",
            (niche_id, platform, action, post_id),
        ).fetchone()
    return row is not None


def count_today_actions(niche_id: str, platform: str, action: str) -> int:
    """Count actions taken today (UTC)."""
    db = get_db()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    row = db.execute(
        "SELECT COUNT(*) as cnt FROM engagement_log WHERE niche_id = ? AND platform = ? AND action = ? AND timestamp LIKE ?",
        (niche_id, platform, action, f"{today}%"),
    ).fetchone()
    return row["cnt"]


def replies_to_author_this_week(niche_id: str, platform: str, author: str) -> int:
    """Count replies to a specific author in the last 7 days."""
    db = get_db()
    from datetime import timedelta
    cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    row = db.execute(
        """SELECT COUNT(*) as cnt FROM engagement_log
           WHERE niche_id = ? AND platform = ? AND action = 'reply'
           AND LOWER(author) = LOWER(?) AND timestamp >= ?""",
        (niche_id, platform, author, cutoff),
    ).fetchone()
    return row["cnt"]


def get_engagement_log(niche_id: str, platform: str, days: int | None = None) -> list[dict]:
    """Get engagement log entries for a niche+platform as dicts.

    If days is set, only returns entries from the last N days.
    """
    db = get_db()
    if days is not None:
        from datetime import timedelta
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        rows = db.execute(
            "SELECT * FROM engagement_log WHERE niche_id = ? AND platform = ? AND timestamp >= ? ORDER BY timestamp",
            (niche_id, platform, cutoff),
        ).fetchall()
    else:
        rows = db.execute(
            "SELECT * FROM engagement_log WHERE niche_id = ? AND platform = ? ORDER BY timestamp",
            (niche_id, platform),
        ).fetchall()
    return [dict(r) for r in rows]


def update_engagement_entry(entry_id: int, **fields):
    """Update specific fields on an engagement log entry."""
    if not fields:
        return
    db = get_db()
    sets = []
    values = []
    for key, val in fields.items():
        sets.append(f"{key} = ?")
        values.append(val)
    values.append(entry_id)
    db.execute(f"UPDATE engagement_log SET {', '.join(sets)} WHERE id = ?", values)
    db.commit()


def get_engaged_authors(niche_id: str, platform: str, action: str) -> set[str]:
    """Get set of authors we've previously engaged with (by action type)."""
    db = get_db()
    rows = db.execute(
        "SELECT DISTINCT COALESCE(author_handle, author) as a FROM engagement_log WHERE niche_id = ? AND platform = ? AND action = ?",
        (niche_id, platform, action),
    ).fetchall()
    return {r["a"] for r in rows if r["a"]}


def get_insights(niche_id: str) -> dict:
    """Load insights for a niche from the insights table."""
    db = get_db()
    row = db.execute(
        "SELECT data FROM insights WHERE niche_id = ?", (niche_id,)
    ).fetchone()
    return json_loads(row["data"], default={}) if row else {}


# ---------------------------------------------------------------------------
# Orchestrator status helpers
# ---------------------------------------------------------------------------

def get_orchestrator_status(config_name: str) -> dict:
    """Load orchestrator status for a config. Returns same structure as JSON."""
    db = get_db()
    rows = db.execute(
        "SELECT * FROM orchestrator_status WHERE config_name = ?",
        (config_name,),
    ).fetchall()

    status = {"scripts": {}}
    for row in rows:
        ss = {
            "last_run": row["last_run"],
            "last_status": row["last_status"],
            "last_exit_code": row["last_exit_code"],
            "last_duration": row["last_duration"],
            "last_metrics": json_loads(row["last_metrics"], default={}),
            "last_error": row["last_error"],
            "runs_today": json_loads(row["runs_today"], default={}),
            "slots_done": json_loads(row["slots_done"], default=[]),
            "consecutive_failures": row["consecutive_failures"],
            "running": bool(row["running"]) if row["running"] is not None else None,
        }
        # Remove None values
        ss = {k: v for k, v in ss.items() if v is not None}
        status["scripts"][row["script_name"]] = ss

    # Load jitter
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    jitter_row = db.execute(
        "SELECT daily_jitter, jitter_date FROM orchestrator_jitter WHERE config_name = ? AND jitter_date = ?",
        (config_name, today),
    ).fetchone()
    if jitter_row:
        status["daily_jitter"] = json_loads(jitter_row["daily_jitter"], default={})
        status["jitter_date"] = jitter_row["jitter_date"]
    else:
        status["daily_jitter"] = {}
        status["jitter_date"] = None

    return status


def save_orchestrator_status(config_name: str, status: dict):
    """Save orchestrator status to DB."""
    db = get_db()
    for script_name, ss in status.get("scripts", {}).items():
        db.execute(
            """INSERT OR REPLACE INTO orchestrator_status
               (config_name, script_name, last_run, last_status, last_exit_code,
                last_duration, last_metrics, last_error, runs_today, slots_done,
                consecutive_failures, running)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                config_name, script_name,
                ss.get("last_run"),
                ss.get("last_status"),
                ss.get("last_exit_code"),
                ss.get("last_duration"),
                json_dumps(ss.get("last_metrics")),
                ss.get("last_error"),
                json_dumps(ss.get("runs_today")),
                json_dumps(ss.get("slots_done")),
                ss.get("consecutive_failures", 0),
                int(ss.get("running", False)) if ss.get("running") is not None else 0,
            ),
        )

    # Save jitter
    if status.get("daily_jitter") and status.get("jitter_date"):
        db.execute(
            """INSERT OR REPLACE INTO orchestrator_jitter
               (config_name, jitter_date, daily_jitter)
               VALUES (?, ?, ?)""",
            (config_name, status["jitter_date"], json_dumps(status["daily_jitter"])),
        )

    db.commit()


# ---------------------------------------------------------------------------
# IG post log helpers
# ---------------------------------------------------------------------------

def log_ig_post(niche_id: str, post_id: int, ig_media_id: str | None = None,
                error: str | None = None):
    """Log an IG cross-post attempt."""
    db = get_db()
    db.execute(
        "INSERT INTO ig_post_log (niche_id, post_id, ig_media_id, error, timestamp) VALUES (?, ?, ?, ?, ?)",
        (niche_id, post_id, ig_media_id, error, datetime.now(timezone.utc).isoformat()),
    )
    db.commit()


def already_ig_posted(niche_id: str, post_id: int) -> bool:
    """Check if a post was already cross-posted to IG (successfully)."""
    db = get_db()
    row = db.execute(
        "SELECT 1 FROM ig_post_log WHERE niche_id = ? AND post_id = ? AND error IS NULL LIMIT 1",
        (niche_id, post_id),
    ).fetchone()
    return row is not None
