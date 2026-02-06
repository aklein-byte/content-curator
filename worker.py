"""
Render Background Worker for @tatamispaces.
Runs engage.py, post.py, and research.py on schedule.
Uses a Render Disk at /data for persistent state.

Deploy as: Render Background Worker with Disk mounted at /data
"""

import sys
import os
import json
import shutil
import asyncio
import logging
from pathlib import Path
from datetime import datetime, timezone, timedelta

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent))

from dotenv import load_dotenv
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [worker] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("worker")

BASE_DIR = Path(__file__).parent
DATA_DIR = Path(os.environ.get("RENDER_DISK_PATH", "/data"))

# Files that need to persist between deploys
PERSISTENT_FILES = [
    "posts.json",
    "engagement-log.json",
    "engagement-drafts.json",
]

# Schedule (UTC hours)
ENGAGE_INTERVAL_HOURS = 6
POST_CHECK_INTERVAL_MINUTES = 30
RESEARCH_HOUR_UTC = 14  # 9 AM ET


def sync_state():
    """
    Sync persistent files between repo and Render Disk.
    On deploy: merge repo posts.json with Disk version (Disk wins for status).
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    for filename in PERSISTENT_FILES:
        repo_file = BASE_DIR / filename
        disk_file = DATA_DIR / filename

        if filename == "posts.json":
            # Special merge: repo has new posts, Disk has status updates
            _merge_posts(repo_file, disk_file)
        else:
            # For logs: keep the Disk version, start fresh if none exists
            if not disk_file.exists() and repo_file.exists():
                shutil.copy2(repo_file, disk_file)
            elif not disk_file.exists():
                disk_file.write_text("[]")

    # Create cookies dir on Disk
    cookies_dir = DATA_DIR / "cookies"
    cookies_dir.mkdir(exist_ok=True)
    os.environ["COOKIES_DIR"] = str(cookies_dir)

    log.info(f"State synced. Disk: {DATA_DIR}")


def _merge_posts(repo_file: Path, disk_file: Path):
    """Merge posts.json: new posts from repo, status updates from Disk."""
    repo_data = {"posts": []}
    disk_data = {"posts": []}

    if repo_file.exists():
        repo_data = json.loads(repo_file.read_text())
    if disk_file.exists():
        disk_data = json.loads(disk_file.read_text())

    # Build lookup of Disk posts by ID
    disk_posts = {p["id"]: p for p in disk_data.get("posts", [])}

    merged = []
    for post in repo_data.get("posts", []):
        pid = post["id"]
        if pid in disk_posts:
            # Disk version wins (has status updates like "posted")
            merged.append(disk_posts.pop(pid))
        else:
            # New post from repo
            merged.append(post)

    # Any posts only on Disk (shouldn't happen normally)
    for p in disk_posts.values():
        merged.append(p)

    merged.sort(key=lambda p: p.get("id", 0))
    result = {"posts": merged}
    disk_file.write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    log.info(f"Posts merged: {len(merged)} total ({len(repo_data.get('posts', []))} from repo)")


async def run_engage():
    """Run engagement script."""
    log.info("=== Running engage.py ===")
    try:
        # Override file paths to use Disk
        os.environ["ENGAGEMENT_LOG"] = str(DATA_DIR / "engagement-log.json")
        os.environ["ENGAGEMENT_DRAFTS"] = str(DATA_DIR / "engagement-drafts.json")

        proc = await asyncio.create_subprocess_exec(
            sys.executable, str(BASE_DIR / "engage.py"),
            "--niche", "tatamispaces",
            cwd=str(BASE_DIR),
            env={**os.environ},
        )
        await proc.wait()
        log.info(f"engage.py exited with code {proc.returncode}")
    except Exception as e:
        log.error(f"engage.py failed: {e}")


async def run_post():
    """Run posting script."""
    log.info("=== Running post.py ===")
    try:
        # Point post.py at the Disk copy of posts.json
        os.environ["POSTS_FILE"] = str(DATA_DIR / "posts.json")

        proc = await asyncio.create_subprocess_exec(
            sys.executable, str(BASE_DIR / "post.py"),
            "--niche", "tatamispaces",
            cwd=str(BASE_DIR),
            env={**os.environ},
        )
        await proc.wait()
        log.info(f"post.py exited with code {proc.returncode}")
    except Exception as e:
        log.error(f"post.py failed: {e}")


async def run_bookmarks():
    """Run bookmarks-to-posts pipeline."""
    log.info("=== Running bookmarks.py ===")
    try:
        os.environ["POSTS_FILE"] = str(DATA_DIR / "posts.json")

        proc = await asyncio.create_subprocess_exec(
            sys.executable, str(BASE_DIR / "bookmarks.py"),
            "--niche", "tatamispaces",
            "--max-drafts", "5",
            "--min-score", "7",
            cwd=str(BASE_DIR),
            env={**os.environ},
        )
        await proc.wait()
        log.info(f"bookmarks.py exited with code {proc.returncode}")
    except Exception as e:
        log.error(f"bookmarks.py failed: {e}")


async def scheduler():
    """Main scheduler loop."""
    sync_state()

    last_engage = datetime.min.replace(tzinfo=timezone.utc)
    last_post_check = datetime.min.replace(tzinfo=timezone.utc)
    last_research_date = None

    log.info("Scheduler started. Waiting 30s before first run...")
    await asyncio.sleep(30)

    # Run engage immediately on startup
    await run_engage()
    last_engage = datetime.now(timezone.utc)

    # Check for posts immediately
    await run_post()
    last_post_check = datetime.now(timezone.utc)

    while True:
        now = datetime.now(timezone.utc)

        # Engage every 6 hours
        if (now - last_engage) >= timedelta(hours=ENGAGE_INTERVAL_HOURS):
            await run_engage()
            last_engage = now

        # Check post queue every 30 minutes
        if (now - last_post_check) >= timedelta(minutes=POST_CHECK_INTERVAL_MINUTES):
            await run_post()
            last_post_check = now

        # Bookmarks once daily at 14:00 UTC (9 AM ET)
        today = now.date()
        if now.hour >= RESEARCH_HOUR_UTC and last_research_date != today:
            await run_bookmarks()
            last_research_date = today

        # Sleep 5 minutes between checks
        await asyncio.sleep(300)


if __name__ == "__main__":
    log.info("tatamispaces worker starting...")
    asyncio.run(scheduler())
