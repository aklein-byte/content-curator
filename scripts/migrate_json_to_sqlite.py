#!/usr/bin/env python3
"""
One-time migration: JSON files → SQLite.

Reads all posts, engagement logs, orchestrator status, IG post logs,
and response logs from JSON files and inserts them into the SQLite DB.

Verifies row counts after migration. Renames JSON files to .json.bak.

Usage:
    python scripts/migrate_json_to_sqlite.py              # full migration
    python scripts/migrate_json_to_sqlite.py --dry-run    # show what would be migrated
    python scripts/migrate_json_to_sqlite.py --no-backup  # skip renaming to .bak
"""

import sys
import json
import argparse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from tools.db import (
    init_db, get_db, close_db,
    json_dumps, insert_post,
)
from tools.common import load_json, niche_log_path
from config.niches import list_niches, get_niche

BASE_DIR = Path(__file__).parent.parent


def migrate_posts(niche_id: str, dry_run: bool = False) -> int:
    """Migrate posts JSON file to DB. Returns count of posts migrated."""
    niche = get_niche(niche_id)
    filename = niche.get("posts_file", "posts.json")
    posts_file = BASE_DIR / filename

    if not posts_file.exists():
        print(f"  [skip] {posts_file.name} does not exist")
        return 0

    data = load_json(posts_file, default={"posts": []})
    if isinstance(data, list):
        data = {"posts": data}

    posts = data.get("posts", [])
    print(f"  {posts_file.name}: {len(posts)} posts")

    if dry_run:
        return len(posts)

    db = get_db()
    count = 0
    for post in posts:
        post_id = post.get("id")
        if post_id is None:
            print(f"    WARNING: post without ID, skipping: {str(post)[:100]}")
            continue

        insert_post(niche_id, post, _commit=False)
        count += 1
    db.commit()

    # Verify count
    row = db.execute(
        "SELECT COUNT(*) as cnt FROM posts WHERE niche_id = ?", (niche_id,)
    ).fetchone()
    db_count = row["cnt"]
    if db_count != count:
        print(f"    WARNING: inserted {count} but DB has {db_count}")
    else:
        print(f"    Migrated {count} posts")

    # Verify museum tweets for museum niche
    if niche_id == "museumstories":
        museum_posts = [p for p in posts if p.get("type") == "museum" and p.get("tweets")]
        total_tweets = sum(len(p["tweets"]) for p in museum_posts)
        db_tweets = db.execute(
            "SELECT COUNT(*) as cnt FROM museum_tweets mt JOIN posts p ON mt.post_id = p.id WHERE p.niche_id = ?",
            (niche_id,),
        ).fetchone()["cnt"]
        print(f"    Museum tweets: {total_tweets} in JSON → {db_tweets} in DB")

    return count


def migrate_engagement_log(niche_id: str, platform: str, filename: str, dry_run: bool = False) -> int:
    """Migrate an engagement log JSON file to DB. Returns count."""
    log_file = niche_log_path(filename, niche_id)

    if not log_file.exists():
        print(f"  [skip] {log_file.name} does not exist")
        return 0

    entries = load_json(log_file, default=[])
    if not isinstance(entries, list):
        print(f"  [skip] {log_file.name} is not a list")
        return 0

    print(f"  {log_file.name}: {len(entries)} entries")

    if dry_run:
        return len(entries)

    db = get_db()
    count = 0
    for entry in entries:
        db.execute(
            """INSERT INTO engagement_log
               (niche_id, platform, action, post_id, author, author_handle,
                score, post_likes, author_followers, query, reason, timestamp,
                reply_id, reply_text, comment, shortcode,
                reply_likes, reply_replies, reply_retweets, checked_at,
                reply_to_tweet_id, our_response_id, replier, our_response, parent_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                niche_id, platform,
                entry.get("action", ""),
                entry.get("post_id"),
                entry.get("author"),
                entry.get("author_handle"),
                entry.get("score"),
                entry.get("post_likes"),
                entry.get("author_followers"),
                entry.get("query"),
                entry.get("reason"),
                entry.get("timestamp", ""),
                entry.get("reply_id"),
                entry.get("reply_text"),
                entry.get("comment"),
                entry.get("shortcode"),
                entry.get("reply_likes"),
                entry.get("reply_replies"),
                entry.get("reply_retweets"),
                entry.get("checked_at"),
                entry.get("reply_to_tweet_id"),
                entry.get("our_response_id"),
                entry.get("replier"),
                entry.get("our_response"),
                entry.get("parent_id"),
            ),
        )
        count += 1

    db.commit()

    # Verify
    row = db.execute(
        "SELECT COUNT(*) as cnt FROM engagement_log WHERE niche_id = ? AND platform = ?",
        (niche_id, platform),
    ).fetchone()
    print(f"    Migrated {count} → DB has {row['cnt']}")
    return count


def migrate_response_log(dry_run: bool = False) -> int:
    """Migrate response-log.json (niche-independent) to engagement_log table."""
    resp_file = BASE_DIR / "response-log.json"
    if not resp_file.exists():
        print(f"  [skip] response-log.json does not exist")
        return 0

    entries = load_json(resp_file, default=[])
    print(f"  response-log.json: {len(entries)} entries")

    if dry_run:
        return len(entries)

    db = get_db()
    count = 0
    for entry in entries:
        # Response logs are X platform, action=respond
        # Determine niche from context — response log was global (tatamispaces era)
        niche_id = "tatamispaces"
        db.execute(
            """INSERT INTO engagement_log
               (niche_id, platform, action, post_id, author, timestamp,
                reply_to_tweet_id, our_response_id, replier, reply_text, our_response,
                parent_id, score)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                niche_id, "x",
                entry.get("action", "respond"),
                entry.get("reply_to_tweet_id"),
                entry.get("replier"),
                entry.get("timestamp", ""),
                entry.get("reply_to_tweet_id"),
                entry.get("our_response_id"),
                entry.get("replier"),
                entry.get("reply_text"),
                entry.get("our_response"),
                entry.get("parent_id"),
                entry.get("score"),
            ),
        )
        count += 1

    db.commit()
    print(f"    Migrated {count} response entries")
    return count


def migrate_orchestrator_status(dry_run: bool = False) -> int:
    """Migrate orchestrator-status*.json files to DB."""
    import glob
    status_files = sorted(BASE_DIR.glob("data/orchestrator-status*.json"))

    if not status_files:
        print("  [skip] No orchestrator status files found")
        return 0

    db = get_db()
    total = 0
    for sf in status_files:
        data = load_json(sf, default={})
        scripts = data.get("scripts", {})
        if not scripts:
            continue

        # Derive config_name from filename
        # orchestrator-status.json -> config
        # orchestrator-status-config-museum.json -> config-museum
        stem = sf.stem  # e.g. "orchestrator-status-config-museum"
        if stem == "orchestrator-status":
            config_name = "config"
        else:
            config_name = stem.replace("orchestrator-status-", "")

        print(f"  {sf.name}: {len(scripts)} scripts (config_name={config_name})")

        if dry_run:
            total += len(scripts)
            continue

        for script_name, ss in scripts.items():
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
                    int(ss.get("running", False)),
                ),
            )
            total += 1

        # Save jitter
        if data.get("daily_jitter") and data.get("jitter_date"):
            db.execute(
                """INSERT OR REPLACE INTO orchestrator_jitter
                   (config_name, jitter_date, daily_jitter)
                   VALUES (?, ?, ?)""",
                (config_name, data["jitter_date"], json_dumps(data["daily_jitter"])),
            )

        db.commit()

    print(f"    Migrated {total} orchestrator script entries")
    return total


def migrate_ig_post_log(niche_id: str, dry_run: bool = False) -> int:
    """Migrate ig-post-log*.json to DB."""
    log_file = niche_log_path("ig-post-log.json", niche_id)
    if not log_file.exists():
        print(f"  [skip] {log_file.name} does not exist")
        return 0

    entries = load_json(log_file, default=[])
    print(f"  {log_file.name}: {len(entries)} entries")

    if dry_run:
        return len(entries)

    db = get_db()
    for entry in entries:
        db.execute(
            "INSERT INTO ig_post_log (niche_id, post_id, ig_media_id, error, timestamp) VALUES (?, ?, ?, ?, ?)",
            (
                niche_id,
                entry.get("post_id"),
                entry.get("ig_media_id"),
                entry.get("error"),
                entry.get("timestamp", ""),
            ),
        )
    db.commit()
    print(f"    Migrated {len(entries)} IG post log entries")
    return len(entries)


def migrate_insights(niche_id: str, dry_run: bool = False) -> int:
    """Migrate insights-*.json to DB."""
    insights_file = BASE_DIR / "data" / f"insights-{niche_id}.json"
    if not insights_file.exists():
        print(f"  [skip] insights-{niche_id}.json does not exist")
        return 0

    data = load_json(insights_file, default={})
    print(f"  insights-{niche_id}.json: {len(data)} keys")

    if dry_run:
        return 1

    db = get_db()
    db.execute(
        "INSERT OR REPLACE INTO insights (niche_id, data, updated_at) VALUES (?, ?, ?)",
        (niche_id, json.dumps(data, ensure_ascii=False, default=str), data.get("updated_at", "")),
    )
    db.commit()
    print(f"    Migrated insights for {niche_id}")
    return 1


def migrate_respond_since_id(dry_run: bool = False) -> int:
    """Migrate respond-since-id.txt to kv_store (for tatamispaces)."""
    f = BASE_DIR / "data" / "respond-since-id.txt"
    if not f.exists():
        print(f"  [skip] respond-since-id.txt does not exist")
        return 0

    val = f.read_text().strip()
    print(f"  respond-since-id.txt: {val}")

    if dry_run:
        return 1

    db = get_db()
    # Save with niche-specific key (respond.py uses respond_since_id_{niche_id})
    db.execute(
        "INSERT OR REPLACE INTO kv_store (key, value) VALUES (?, ?)",
        ("respond_since_id_tatamispaces", val),
    )
    db.commit()
    print(f"    Migrated respond-since-id for tatamispaces")
    return 1


def backup_json_files(dry_run: bool = False, no_backup: bool = False):
    """Rename migrated JSON files to .json.bak."""
    if no_backup:
        print("\n  Skipping backup (--no-backup)")
        return

    files_to_backup = []

    # Posts files
    for niche_id in list_niches():
        niche = get_niche(niche_id)
        filename = niche.get("posts_file", "posts.json")
        f = BASE_DIR / filename
        if f.exists():
            files_to_backup.append(f)

    # Engagement logs
    for niche_id in list_niches():
        for filename in ["engagement-log.json", "ig-engagement-log.json", "ig-post-log.json"]:
            f = niche_log_path(filename, niche_id)
            if f.exists():
                files_to_backup.append(f)

    # Response log
    f = BASE_DIR / "response-log.json"
    if f.exists():
        files_to_backup.append(f)

    # Orchestrator status files
    for sf in BASE_DIR.glob("data/orchestrator-status*.json"):
        files_to_backup.append(sf)

    # Insights files
    for sf in BASE_DIR.glob("data/insights-*.json"):
        files_to_backup.append(sf)

    # Bluesky logs
    for sf in BASE_DIR.glob("data/bluesky-engagement-log-*.json"):
        files_to_backup.append(sf)
    for sf in BASE_DIR.glob("data/bluesky-response-log-*.json"):
        files_to_backup.append(sf)

    # Respond since ID
    f = BASE_DIR / "data" / "respond-since-id.txt"
    if f.exists():
        files_to_backup.append(f)

    print(f"\n  Files to backup: {len(files_to_backup)}")
    for f in files_to_backup:
        bak = f.with_suffix(f.suffix + ".bak")
        if dry_run:
            print(f"    [DRY] {f.name} → {bak.name}")
        else:
            f.rename(bak)
            print(f"    {f.name} → {bak.name}")


def main():
    parser = argparse.ArgumentParser(description="Migrate JSON files to SQLite")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be migrated")
    parser.add_argument("--no-backup", action="store_true", help="Don't rename JSON files to .bak")
    args = parser.parse_args()

    dry_run = args.dry_run
    label = " [DRY RUN]" if dry_run else ""
    print(f"=== JSON → SQLite Migration{label} ===\n")

    if not dry_run:
        init_db()

    niches = list_niches()
    print(f"Niches: {niches}\n")

    # 1. Migrate posts
    print("--- Posts ---")
    for niche_id in niches:
        migrate_posts(niche_id, dry_run)

    # 2. Migrate engagement logs (X)
    print("\n--- X Engagement Logs ---")
    for niche_id in niches:
        migrate_engagement_log(niche_id, "x", "engagement-log.json", dry_run)

    # 3. Migrate IG engagement logs
    print("\n--- IG Engagement Logs ---")
    for niche_id in niches:
        migrate_engagement_log(niche_id, "ig", "ig-engagement-log.json", dry_run)

    # 4. Migrate Bluesky engagement logs
    print("\n--- Bluesky Engagement Logs ---")
    for niche_id in niches:
        # Bluesky engagement logs live in data/ directory
        bsky_file = BASE_DIR / "data" / f"bluesky-engagement-log-{niche_id}.json"
        if bsky_file.exists():
            entries = load_json(bsky_file, default=[])
            print(f"  {bsky_file.name}: {len(entries)} entries")
            if not dry_run and entries:
                db = get_db()
                for entry in entries:
                    db.execute(
                        """INSERT INTO engagement_log
                           (niche_id, platform, action, post_id, author, author_handle,
                            score, post_likes, author_followers, query, reason, timestamp,
                            reply_id, reply_text, comment, shortcode)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            niche_id, "bluesky",
                            entry.get("action", ""),
                            entry.get("post_id"),
                            entry.get("author"),
                            entry.get("author_handle"),
                            entry.get("score"),
                            entry.get("post_likes"),
                            entry.get("author_followers"),
                            entry.get("query"),
                            entry.get("reason"),
                            entry.get("timestamp", ""),
                            entry.get("reply_id"),
                            entry.get("reply_text"),
                            entry.get("comment"),
                            entry.get("shortcode"),
                        ),
                    )
                db.commit()
                print(f"    Migrated {len(entries)} bluesky entries")

    # 4b. Migrate Bluesky response logs
    print("\n--- Bluesky Response Logs ---")
    for niche_id in niches:
        bsky_resp = BASE_DIR / "data" / f"bluesky-response-log-{niche_id}.json"
        if bsky_resp.exists():
            entries = load_json(bsky_resp, default=[])
            print(f"  {bsky_resp.name}: {len(entries)} entries")
            if not dry_run and entries:
                db = get_db()
                for entry in entries:
                    db.execute(
                        """INSERT INTO engagement_log
                           (niche_id, platform, action, post_id, author, timestamp,
                            reply_to_tweet_id, our_response_id, replier, reply_text,
                            our_response, parent_id, score)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            niche_id, "bluesky", "respond",
                            entry.get("reply_to_tweet_id", entry.get("post_id")),
                            entry.get("replier"),
                            entry.get("timestamp", ""),
                            entry.get("reply_to_tweet_id"),
                            entry.get("our_response_id"),
                            entry.get("replier"),
                            entry.get("reply_text"),
                            entry.get("our_response"),
                            entry.get("parent_id"),
                            entry.get("score"),
                        ),
                    )
                db.commit()
                print(f"    Migrated {len(entries)} bluesky response entries")

    # 5. Migrate response log (X)
    print("\n--- Response Log ---")
    migrate_response_log(dry_run)

    # 5. Migrate orchestrator status
    print("\n--- Orchestrator Status ---")
    migrate_orchestrator_status(dry_run)

    # 6. Migrate IG post logs
    print("\n--- IG Post Logs ---")
    for niche_id in niches:
        migrate_ig_post_log(niche_id, dry_run)

    # 7. Migrate insights
    print("\n--- Insights ---")
    for niche_id in niches:
        migrate_insights(niche_id, dry_run)

    # 8. Migrate respond-since-id
    print("\n--- KV Store ---")
    migrate_respond_since_id(dry_run)

    # 9. Verification summary
    if not dry_run:
        print("\n--- Verification ---")
        db = get_db()
        for table in ["posts", "museum_tweets", "engagement_log", "orchestrator_status",
                       "ig_post_log", "insights", "kv_store"]:
            row = db.execute(f"SELECT COUNT(*) as cnt FROM {table}").fetchone()
            print(f"  {table}: {row['cnt']} rows")

        # Per-niche post breakdown
        rows = db.execute(
            "SELECT niche_id, status, COUNT(*) as cnt FROM posts GROUP BY niche_id, status ORDER BY niche_id, status"
        ).fetchall()
        print("\n  Posts by niche/status:")
        for r in rows:
            print(f"    {r['niche_id']:20s} {r['status']:20s} {r['cnt']:5d}")

    # 10. Backup originals
    print("\n--- Backup ---")
    backup_json_files(dry_run, args.no_backup)

    if not dry_run:
        close_db()

    print(f"\n=== Migration {'preview' if dry_run else 'complete'} ===")


if __name__ == "__main__":
    main()
