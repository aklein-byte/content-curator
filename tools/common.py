"""
Shared utilities for content-curator scripts.

Consolidates duplicated patterns: JSON I/O, lockfiles, notifications,
async delays, and logging setup.
"""

import os
import sys
import json
import fcntl
import random
import asyncio
import logging
import platform
import subprocess
from pathlib import Path
from typing import IO

BASE_DIR = Path(__file__).parent.parent


def setup_logging(name: str) -> logging.Logger:
    """Configure logging and return a named logger."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
    )
    return logging.getLogger(name)


# --- JSON I/O (atomic writes via tmp+rename) ---

def load_json(path: Path, default=None):
    """Load JSON from a file. Returns default (or empty list) if missing/corrupt."""
    if default is None:
        default = []
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as e:
        logging.getLogger("common").warning(f"Failed to load {path}: {e}")
        return default


def save_json(path: Path, data, lock: bool = False) -> None:
    """Atomic JSON write: write to tmp file then rename.
    If lock=True, acquires an exclusive flock on path.lock during the write
    to prevent concurrent read-modify-write races (e.g. dashboard vs post.py).
    """
    import fcntl
    path.parent.mkdir(parents=True, exist_ok=True)

    lockfile = path.parent / f".{path.name}.lock"
    fd = None
    if lock:
        lockfile.touch(exist_ok=True)
        fd = open(lockfile, "r")
        fcntl.flock(fd, fcntl.LOCK_EX)

    try:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False, default=str))
        tmp.rename(path)
    finally:
        if fd:
            fcntl.flock(fd, fcntl.LOCK_UN)
            fd.close()


# --- Lockfile ---

def acquire_lock(lockfile: Path) -> IO | None:
    """Try to acquire an exclusive lock. Returns file descriptor or None if locked."""
    fd = open(lockfile, "w")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd
    except BlockingIOError:
        fd.close()
        return None


def release_lock(fd: IO) -> None:
    """Release a lockfile acquired with acquire_lock."""
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
        fd.close()
    except Exception:
        pass


# --- Notifications ---

def notify(title: str, message: str, priority: str = "default") -> None:
    """Send push notification via ntfy.sh with macOS fallback."""
    ntfy_topic = os.getenv("NTFY_TOPIC", "wp-tatami-orchestrator")
    tags = "warning" if priority == "high" else "white_check_mark"

    # Try ntfy.sh first
    try:
        import urllib.request
        req = urllib.request.Request(
            f"https://ntfy.sh/{ntfy_topic}",
            data=message.encode(),
            headers={"Title": title, "Priority": priority, "Tags": tags},
        )
        urllib.request.urlopen(req, timeout=5)
        return
    except Exception:
        pass

    # Fallback to terminal-notifier on macOS
    if platform.system() == "Darwin":
        try:
            subprocess.run(
                ["terminal-notifier", "-title", title, "-message", message,
                 "-group", "orchestrator", "-sound", "Basso"],
                timeout=5, capture_output=True,
            )
        except (FileNotFoundError, Exception):
            pass


# --- Config ---

_config_cache = None


def load_config() -> dict:
    """Load config.json (cached after first call)."""
    global _config_cache
    if _config_cache is not None:
        return _config_cache
    config_path = BASE_DIR / "config.json"
    if config_path.exists():
        _config_cache = json.loads(config_path.read_text())
    else:
        _config_cache = {}
    return _config_cache


# --- Async delay ---

async def random_delay(label: str = "", min_sec: float = 30, max_sec: float = 120) -> None:
    """Sleep a random duration to look human."""
    wait = random.uniform(min_sec, max_sec)
    if label:
        logging.getLogger("common").info(f"Waiting {wait:.0f}s before {label}...")
    await asyncio.sleep(wait)
