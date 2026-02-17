#!/usr/bin/env python3
"""
Orchestrator for @tatamispaces automation.

Replaces 5 separate launchd jobs with one coordinated system.
Runs every 5 minutes via launchd. Each heartbeat checks what's due
and spawns scripts as subprocesses.

Usage:
    python orchestrator.py              # Run one heartbeat
    python orchestrator.py status       # Show today's activity
    python orchestrator.py --dry-run    # Show what would run without running it
"""

import sys
import os
import json
import re
import subprocess
import fcntl
import random
import logging
import argparse
from pathlib import Path
from datetime import datetime, timedelta, date, time as dt_time
from zoneinfo import ZoneInfo

BASE_DIR = Path(__file__).parent
DEFAULT_CONFIG_FILE = BASE_DIR / "config.json"
STATUS_FILE = BASE_DIR / "data" / "orchestrator-status.json"
LOCKFILE = BASE_DIR / ".orchestrator.lock"
LOG_DIR = BASE_DIR / "logs"

# Set by main() from --config flag
CONFIG_FILE = DEFAULT_CONFIG_FILE

ET = ZoneInfo("America/New_York")
WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]

LOG_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stdout,
)
log = logging.getLogger("orchestrator")


# --- JSON I/O ---

def load_config() -> dict:
    if not CONFIG_FILE.exists():
        log.error(f"Config not found: {CONFIG_FILE}")
        sys.exit(1)
    return json.loads(CONFIG_FILE.read_text())


def load_status() -> dict:
    if STATUS_FILE.exists():
        try:
            return json.loads(STATUS_FILE.read_text())
        except json.JSONDecodeError:
            log.warning("Corrupt status.json, starting fresh")
    return {"scripts": {}, "daily_jitter": {}, "jitter_date": None}


def save_status(status: dict):
    STATUS_FILE.parent.mkdir(exist_ok=True)
    tmp = STATUS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(status, indent=2, default=str))
    tmp.rename(STATUS_FILE)


# --- Jitter ---

def get_daily_jitter(config: dict, status: dict, today_str: str) -> dict:
    """Generate or reuse today's jitter offsets (minutes) per script."""
    if status.get("jitter_date") == today_str and status.get("daily_jitter"):
        return status["daily_jitter"]

    jitter = {}
    for name, sc in config["scripts"].items():
        jitter_max = sc.get("jitter_minutes", 0)
        if sc["type"] == "scheduled":
            jitter[name] = [random.randint(-jitter_max, jitter_max) for _ in sc.get("times_et", [])]
        elif sc["type"] == "weekly":
            jitter[name] = [random.randint(-jitter_max, jitter_max)]
        else:
            jitter[name] = []

    status["daily_jitter"] = jitter
    status["jitter_date"] = today_str
    return jitter


# --- Scheduling ---

def parse_time_et(t: str) -> dt_time:
    """Parse 'HH:MM' string to time object."""
    h, m = t.split(":")
    return dt_time(int(h), int(m))


def should_run(name: str, sc: dict, status: dict, now_et: datetime, jitter: dict) -> tuple[bool, str]:
    """Decide if a script should run this heartbeat. Returns (should_run, reason)."""
    if not sc.get("enabled", True):
        return False, "disabled"

    script_status = status.get("scripts", {}).get(name, {})
    last_run_str = script_status.get("last_run")
    last_run = datetime.fromisoformat(last_run_str) if last_run_str else None

    # Don't re-run if already running (lockfile handles this too, but skip the subprocess)
    if script_status.get("running"):
        return False, "already running"

    today = now_et.date()

    if sc["type"] == "interval":
        interval = timedelta(minutes=sc.get("interval_minutes", 30))
        if last_run is None:
            return True, "never run"
        if now_et - last_run >= interval:
            return True, f"interval {sc['interval_minutes']}m elapsed"
        return False, f"next in {int((interval - (now_et - last_run)).total_seconds() / 60)}m"

    elif sc["type"] == "scheduled":
        times = sc.get("times_et", [])
        jitter_offsets = jitter.get(name, [0] * len(times))
        runs_today = script_status.get("runs_today", {}).get(str(today), 0)

        for i, t_str in enumerate(times):
            t = parse_time_et(t_str)
            offset = jitter_offsets[i] if i < len(jitter_offsets) else 0
            scheduled_dt = datetime.combine(today, t, tzinfo=ET) + timedelta(minutes=offset)

            if now_et >= scheduled_dt:
                # Check if this specific slot was already handled
                slot_key = f"{today}_{i}"
                if slot_key in script_status.get("slots_done", []):
                    continue

                # Catch-up: if we missed it, run now (max 1 catch-up)
                if last_run and last_run.date() == today and runs_today >= (i + 1):
                    continue

                return True, f"scheduled {t_str} (jitter {offset:+d}m)"

        return False, "no scheduled slot due"

    elif sc["type"] == "weekly":
        target_day = WEEKDAYS.index(sc.get("day", "monday").lower())
        if today.weekday() != target_day:
            return False, f"not {sc['day']}"

        t = parse_time_et(sc.get("time_et", "10:00"))
        jitter_offsets = jitter.get(name, [0])
        offset = jitter_offsets[0]
        scheduled_dt = datetime.combine(today, t, tzinfo=ET) + timedelta(minutes=offset)

        if now_et < scheduled_dt:
            return False, f"not yet (scheduled {t} {offset:+d}m)"

        # Already ran this week?
        if last_run and last_run.date() == today:
            return False, "already ran today"

        return True, f"weekly {sc['day']} {sc.get('time_et')} (jitter {offset:+d}m)"

    return False, f"unknown type: {sc['type']}"


# --- Script execution ---

def build_args(name: str, sc: dict, config: dict) -> list[str]:
    """Build command-line args for a script."""
    args = ["--niche", config.get("niche", "tatamispaces")]

    # Limits from config
    for flag, val in sc.get("limits", {}).items():
        args.extend([flag, str(val)])

    # Extra args
    args.extend(sc.get("extra_args", []))

    return args


def run_script(name: str, sc: dict, config: dict, dry_run: bool = False) -> dict:
    """Execute a script as subprocess. Returns result dict."""
    python = str(BASE_DIR / config.get("python", "venv/bin/python"))
    script_file = str(BASE_DIR / sc["file"])
    timeout = sc.get("timeout_seconds", 300)
    cli_args = build_args(name, sc, config)

    cmd = [python, script_file] + cli_args
    log.info(f"Running: {' '.join(cmd)} (timeout {timeout}s)")

    if dry_run:
        return {
            "status": "dry_run",
            "command": cmd,
            "stdout": "",
            "stderr": "",
            "exit_code": 0,
            "duration_seconds": 0,
        }

    start = datetime.now(ET)
    try:
        result = subprocess.run(
            cmd,
            cwd=str(BASE_DIR),
            capture_output=True,
            text=True,
            timeout=timeout,
            env={
                **os.environ,
                "PYTHONUNBUFFERED": "1",
                "PATH": f"{BASE_DIR / 'venv' / 'bin'}:/usr/local/bin:/usr/bin:/bin:/opt/homebrew/bin",
            },
        )
        duration = (datetime.now(ET) - start).total_seconds()
        return {
            "status": "success" if result.returncode == 0 else "failed",
            "command": cmd,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "exit_code": result.returncode,
            "duration_seconds": round(duration, 1),
        }
    except subprocess.TimeoutExpired as e:
        duration = (datetime.now(ET) - start).total_seconds()
        return {
            "status": "timeout",
            "command": cmd,
            "stdout": (e.stdout or b"").decode() if isinstance(e.stdout, bytes) else (e.stdout or ""),
            "stderr": (e.stderr or b"").decode() if isinstance(e.stderr, bytes) else (e.stderr or ""),
            "exit_code": -1,
            "duration_seconds": round(duration, 1),
        }
    except Exception as e:
        duration = (datetime.now(ET) - start).total_seconds()
        return {
            "status": "error",
            "command": cmd,
            "stdout": "",
            "stderr": str(e),
            "exit_code": -1,
            "duration_seconds": round(duration, 1),
        }


# --- Output parsing ---

OUTPUT_PATTERNS = {
    "post": [
        (r"Posted successfully: (.+)", "posted_url"),
        (r"No posts ready", "no_posts_ready"),
        (r"IG cross-post done \((\d+) images?\)", "ig_images"),
    ],
    "ig_post": [
        (r"Instagram: (\d+) post", "ig_posts_crossposted"),
        (r"Found (\d+) post\(s\) to cross-post", "ig_posts_found"),
    ],
    "engage": [
        (r"Done\. Likes: (\d+), Replies: (\d+), Follows: (\d+)", "summary"),
    ],
    "ig_engage": [
        (r"IG engage: (\d+) likes, (\d+) comments, (\d+) follows", "summary"),
    ],
    "bookmarks": [
        (r"New drafts created:\s*(\d+)", "drafts_created"),
        (r"Bookmarks fetched:\s*(\d+)", "bookmarks_fetched"),
    ],
    "thread": [
        (r"Thread posted: (.+?) \((\d+) tweets\)", "thread_posted"),
        (r"DRY RUN", "dry_run"),
    ],
    "respond": [
        (r"Done\. Responses: (\d+)", "responses"),
        (r"Found (\d+) new replies", "replies_found"),
    ],
    "audit": [
        (r"Unfollowed (\d+)/(\d+) this run \((\d+) remaining\)", "unfollowed"),
        (r"Keep:\s+(\d+)", "keep_count"),
        (r"Recommend unfollow:\s+(\d+)", "unfollow_count"),
    ],
}


def parse_output(name: str, stdout: str, exit_code: int) -> dict:
    """Extract metrics from script stdout."""
    metrics = {}
    patterns = OUTPUT_PATTERNS.get(name, [])

    for pattern, label in patterns:
        match = re.search(pattern, stdout)
        if match:
            groups = match.groups()
            if len(groups) == 1:
                metrics[label] = groups[0]
            else:
                metrics[label] = list(groups)

    # Also grab last 5 non-empty lines as summary
    lines = [l.strip() for l in stdout.strip().splitlines() if l.strip()]
    metrics["last_lines"] = lines[-5:] if lines else []

    return metrics


# --- Status update ---

def update_status(status: dict, name: str, result: dict, metrics: dict, now_et: datetime):
    """Update status.json with run results."""
    if "scripts" not in status:
        status["scripts"] = {}

    today_str = str(now_et.date())
    ss = status["scripts"].get(name, {})

    ss["last_run"] = now_et.isoformat()
    ss["last_status"] = result["status"]
    ss["last_exit_code"] = result["exit_code"]
    ss["last_duration"] = result["duration_seconds"]
    ss["last_metrics"] = metrics

    # Track runs per day
    runs_today = ss.get("runs_today", {})
    runs_today[today_str] = runs_today.get(today_str, 0) + 1
    ss["runs_today"] = runs_today

    # Track scheduled slots done today
    if "slot_index" in result:
        slots_done = ss.get("slots_done", [])
        slot_key = f"{today_str}_{result['slot_index']}"
        if slot_key not in slots_done:
            slots_done.append(slot_key)
        ss["slots_done"] = slots_done

    # Consecutive failures
    if result["status"] in ("failed", "timeout", "error"):
        ss["consecutive_failures"] = ss.get("consecutive_failures", 0) + 1
    else:
        ss["consecutive_failures"] = 0

    # Last stderr (truncated)
    if result.get("stderr"):
        ss["last_error"] = result["stderr"][-500:]

    status["scripts"][name] = ss


# --- Notifications ---

def _build_success_summary(name: str, metrics: dict, summary_parts: list) -> str:
    """Build a notification body for a successful script run. Returns empty string to skip."""
    if name == "post":
        lines = [l for l in summary_parts if l.strip()]
        return "\n".join(lines) if lines else ""
    if name == "ig_post":
        n = metrics.get("ig_posts_crossposted", "0")
        return f"Cross-posted {n} to Instagram" if n != "0" else ""
    if name == "engage":
        s = metrics.get("summary")
        if s and isinstance(s, list) and len(s) == 3:
            return f"Likes: {s[0]}, Replies: {s[1]}, Follows: {s[2]}"
        return "\n".join(summary_parts) if summary_parts else "Engagement run complete"
    if name == "ig_engage":
        s = metrics.get("summary")
        if s and isinstance(s, list) and len(s) == 3:
            return f"Likes: {s[0]}, Comments: {s[1]}, Follows: {s[2]}"
        return "\n".join(summary_parts) if summary_parts else "IG engagement run complete"
    if name == "bookmarks":
        drafts = metrics.get("drafts_created", "0")
        fetched = metrics.get("bookmarks_fetched", "0")
        return f"Fetched {fetched} bookmarks, {drafts} new drafts" if fetched != "0" else ""
    if name == "respond":
        r = metrics.get("responses", "0")
        return f"Sent {r} responses" if r != "0" else ""
    if name == "thread":
        tp = metrics.get("thread_posted")
        if tp and isinstance(tp, list) and len(tp) == 2:
            return f"Thread: {tp[0]} ({tp[1]} tweets)"
        return ""
    if name == "audit":
        u = metrics.get("unfollowed")
        if u and isinstance(u, list) and len(u) == 3:
            return f"Unfollowed {u[0]}/{u[1]} ({u[2]} remaining)"
        k = metrics.get("keep_count", "?")
        return f"Audit complete, keeping {k}"
    return ""


def notify(title: str, message: str, priority: str = "high"):
    """Send push notification via ntfy.sh (works on VPS + phone) with macOS fallback."""
    import platform
    config = load_config()
    ntfy_topic = config.get("ntfy_topic") or os.getenv("NTFY_TOPIC", "wp-tatami-orchestrator")
    tags = "warning" if priority == "high" else "white_check_mark"

    # Try ntfy.sh first (works everywhere)
    try:
        import urllib.request
        req = urllib.request.Request(
            f"https://ntfy.sh/{ntfy_topic}",
            data=message.encode(),
            headers={"Title": title, "Priority": priority, "Tags": tags},
        )
        urllib.request.urlopen(req, timeout=5)
        return
    except Exception as e:
        log.warning(f"ntfy.sh failed: {e}")

    # Fallback to terminal-notifier on macOS
    if platform.system() == "Darwin":
        try:
            subprocess.run(
                ["terminal-notifier", "-title", title, "-message", message,
                 "-group", "orchestrator", "-sound", "Basso"],
                timeout=5, capture_output=True,
            )
        except FileNotFoundError:
            log.warning("terminal-notifier not found, skipping notification")
        except Exception as e:
            log.warning(f"macOS notification failed: {e}")


def notify_if_needed(name: str, result: dict, status: dict, config: dict):
    """Send alert on consecutive failures."""
    notif_config = config.get("notifications", {})
    if not notif_config.get("on_failure", True):
        return

    threshold = notif_config.get("consecutive_failures_alert", 3)
    ss = status.get("scripts", {}).get(name, {})
    consecutive = ss.get("consecutive_failures", 0)

    if result["status"] in ("failed", "timeout", "error"):
        niche_label = config.get("niche", "tatami")
        if consecutive >= threshold:
            notify(
                f"{niche_label}: {name} failing",
                f"{consecutive} consecutive failures. Last: {result['status']}",
            )
        elif consecutive == 1:
            log.warning(f"{name} failed: {result['status']}")


# --- Aggregate stats ---

def aggregate_today_stats(now_et: datetime, niche_id: str = None) -> dict:
    """Read engagement logs and count today's actions."""
    today_str = str(now_et.date())
    stats = {"x_likes": 0, "x_replies": 0, "x_follows": 0,
             "ig_likes": 0, "ig_comments": 0, "ig_follows": 0,
             "x_posts": 0, "ig_posts": 0, "drafts_created": 0,
             "x_responses": 0}

    # Resolve niche-aware file suffixes
    if niche_id is None:
        niche_id = load_config().get("niche", "tatamispaces")
    log_suffix = f"-{niche_id}" if niche_id != "tatamispaces" else ""

    # X engagement log
    eng_file = BASE_DIR / f"engagement-log{log_suffix}.json"
    if eng_file.exists():
        try:
            entries = json.loads(eng_file.read_text())
            for e in entries:
                ts = e.get("timestamp", "")
                if not ts.startswith(today_str):
                    continue
                action = e.get("action", "")
                if action == "like":
                    stats["x_likes"] += 1
                elif action == "reply":
                    stats["x_replies"] += 1
                elif action == "follow":
                    stats["x_follows"] += 1
        except Exception:
            pass

    # IG engagement log
    ig_eng_file = BASE_DIR / f"ig-engagement-log{log_suffix}.json"
    if ig_eng_file.exists():
        try:
            entries = json.loads(ig_eng_file.read_text())
            for e in entries:
                ts = e.get("timestamp", "")
                if not ts.startswith(today_str):
                    continue
                action = e.get("action", "")
                if action == "like":
                    stats["ig_likes"] += 1
                elif action == "comment":
                    stats["ig_comments"] += 1
                elif action == "follow":
                    stats["ig_follows"] += 1
        except Exception:
            pass

    # Posts — resolve from niche config
    from config.niches import get_niche
    niche_cfg = get_niche(niche_id)
    posts_file = BASE_DIR / niche_cfg.get("posts_file", "posts.json")
    if posts_file.exists():
        try:
            data = json.loads(posts_file.read_text())
            for p in data.get("posts", []):
                posted_at = p.get("posted_at", "")
                if posted_at and posted_at.startswith(today_str):
                    stats["x_posts"] += 1
                ig_posted_at = p.get("ig_posted_at", "")
                if ig_posted_at and ig_posted_at.startswith(today_str):
                    stats["ig_posts"] += 1
        except Exception:
            pass

    # Responses
    resp_file = BASE_DIR / "response-log.json"
    if resp_file.exists():
        try:
            entries = json.loads(resp_file.read_text())
            for e in entries:
                ts = e.get("timestamp", "")
                if ts.startswith(today_str):
                    stats["x_responses"] += 1
        except Exception:
            pass

    return stats


# --- Status display ---

def print_status(status: dict, now_et: datetime):
    """Pretty-print orchestrator status."""
    today_str = str(now_et.date())
    config = load_config()
    niche_id = config.get("niche", "tatamispaces")
    print(f"\n  {niche_id} orchestrator — {now_et.strftime('%a %b %d %I:%M %p ET')}")
    print(f"  {'=' * 55}")

    # Aggregate stats
    stats = aggregate_today_stats(now_et, niche_id=niche_id)
    print(f"\n  Today's activity:")
    print(f"    X:  {stats['x_posts']} posts | {stats['x_likes']} likes | {stats['x_replies']} replies | {stats['x_follows']} follows | {stats['x_responses']} responses")
    print(f"    IG: {stats['ig_posts']} posts | {stats['ig_likes']} likes | {stats['ig_comments']} comments | {stats['ig_follows']} follows")

    # Per-script status
    print(f"\n  Script status:")
    for name, ss in status.get("scripts", {}).items():
        last_run = ss.get("last_run", "never")
        if last_run != "never":
            try:
                lr = datetime.fromisoformat(last_run)
                ago = now_et - lr
                if ago.total_seconds() < 60:
                    ago_str = "just now"
                elif ago.total_seconds() < 3600:
                    ago_str = f"{int(ago.total_seconds() / 60)}m ago"
                else:
                    ago_str = f"{int(ago.total_seconds() / 3600)}h ago"
            except Exception:
                ago_str = last_run
        else:
            ago_str = "never"

        status_icon = {"success": "+", "failed": "X", "timeout": "!", "error": "X", "dry_run": "~"}.get(ss.get("last_status", ""), "?")
        runs_today = ss.get("runs_today", {}).get(today_str, 0)
        cons_fail = ss.get("consecutive_failures", 0)
        fail_str = f" ({cons_fail} consecutive failures)" if cons_fail > 0 else ""

        print(f"    [{status_icon}] {name:12s}  last: {ago_str:10s}  today: {runs_today}x{fail_str}")

        # Show last metrics summary
        metrics = ss.get("last_metrics", {})
        if name == "engage" and metrics.get("summary"):
            s = metrics["summary"]
            if isinstance(s, list) and len(s) == 3:
                print(f"        -> {s[0]} likes, {s[1]} replies, {s[2]} follows")
            else:
                print(f"        -> {s}")
        elif name == "ig_engage" and metrics.get("summary"):
            s = metrics["summary"]
            if isinstance(s, list) and len(s) == 3:
                print(f"        -> {s[0]} likes, {s[1]} comments, {s[2]} follows")
            else:
                print(f"        -> {s}")
        elif metrics.get("posted_url"):
            print(f"        -> {metrics['posted_url']}")
        elif metrics.get("no_posts_ready") is not None:
            print(f"        -> no posts ready")
        elif metrics.get("ig_posts_crossposted"):
            print(f"        -> {metrics['ig_posts_crossposted']} cross-posted to IG")
        elif metrics.get("drafts_created"):
            print(f"        -> {metrics['drafts_created']} drafts created")
        elif metrics.get("thread_posted"):
            t = metrics["thread_posted"]
            if isinstance(t, list) and len(t) == 2:
                print(f"        -> {t[1]} tweets: {t[0]}")
            else:
                print(f"        -> {t}")
        elif metrics.get("responses") is not None:
            print(f"        -> {metrics['responses']} responses sent")

    # Jitter info
    jitter = status.get("daily_jitter", {})
    if jitter:
        print(f"\n  Today's jitter offsets:")
        config = load_config()
        for name, offsets in jitter.items():
            sc = config["scripts"].get(name, {})
            times = sc.get("times_et", [sc.get("time_et", "")])
            if isinstance(times, str):
                times = [times]
            parts = []
            for i, t in enumerate(times):
                off = offsets[i] if i < len(offsets) else 0
                parts.append(f"{t}{off:+d}m")
            if parts:
                print(f"    {name:12s}  {', '.join(parts)}")

    print()


# --- Heartbeat ---

def heartbeat(config: dict, dry_run: bool = False):
    """Main orchestrator loop: check all scripts, run what's due."""
    now_et = datetime.now(ET)
    today_str = str(now_et.date())
    status = load_status()

    # Clean old runs_today and slots_done entries (keep last 7 days)
    cutoff = str((now_et - timedelta(days=7)).date())
    for ss in status.get("scripts", {}).values():
        if "runs_today" in ss:
            ss["runs_today"] = {k: v for k, v in ss["runs_today"].items() if k >= cutoff}
        if "slots_done" in ss:
            ss["slots_done"] = [s for s in ss["slots_done"] if s >= cutoff]

    jitter = get_daily_jitter(config, status, today_str)

    log.info(f"Heartbeat at {now_et.strftime('%H:%M:%S ET')} {'[DRY RUN]' if dry_run else ''}")

    ran_any = False
    for name, sc in config["scripts"].items():
        should, reason = should_run(name, sc, status, now_et, jitter)

        if not should:
            log.info(f"  {name}: skip ({reason})")
            continue

        log.info(f"  {name}: due ({reason})")
        ran_any = True

        result = run_script(name, sc, config, dry_run=dry_run)

        if dry_run:
            log.info(f"  {name}: would run: {' '.join(result['command'])}")
            continue

        metrics = parse_output(name, result["stdout"], result["exit_code"])

        # Find which slot this was (for scheduled scripts)
        if sc["type"] == "scheduled":
            times = sc.get("times_et", [])
            jitter_offsets = jitter.get(name, [0] * len(times))
            for i, t_str in enumerate(times):
                t = parse_time_et(t_str)
                offset = jitter_offsets[i] if i < len(jitter_offsets) else 0
                scheduled_dt = datetime.combine(now_et.date(), t, tzinfo=ET) + timedelta(minutes=offset)
                slot_key = f"{today_str}_{i}"
                if now_et >= scheduled_dt and slot_key not in status.get("scripts", {}).get(name, {}).get("slots_done", []):
                    result["slot_index"] = i
                    break

        update_status(status, name, result, metrics, now_et)
        notify_if_needed(name, result, status, config)

        if result["status"] != "success":
            log.warning(f"  {name}: {result['status']} (exit {result['exit_code']}, {result['duration_seconds']}s)")
            if result.get("stderr"):
                for line in result["stderr"].strip().splitlines()[-3:]:
                    log.warning(f"    stderr: {line}")
        else:
            log.info(f"  {name}: ok ({result['duration_seconds']}s)")
            summary_parts = []
            if metrics.get("last_lines"):
                for line in metrics["last_lines"][-2:]:
                    cleaned = re.sub(r"^\d{2}:\d{2}:\d{2}\s+(INFO|WARNING|ERROR)\s+", "", line)
                    log.info(f"    {cleaned}")
                    summary_parts.append(cleaned)

            # Send success notification for action scripts
            notif_body = _build_success_summary(name, metrics, summary_parts)
            if notif_body:
                niche_label = config.get("niche", "tatami")
                notify(f"{niche_label}: {name}", notif_body, priority="default")

    if not ran_any and not dry_run:
        log.info("  Nothing to run this heartbeat.")

    save_status(status)
    log.info("Heartbeat complete.")


# --- Main ---

def main():
    parser = argparse.ArgumentParser(description="Social media orchestrator")
    parser.add_argument("command", nargs="?", default="run", choices=["run", "status"],
                        help="'run' for heartbeat, 'status' for today's summary")
    parser.add_argument("--dry-run", action="store_true", help="Show what would run without running")
    parser.add_argument("--config", default=None, help="Config file (default: config.json)")
    args = parser.parse_args()

    global CONFIG_FILE, STATUS_FILE, LOCKFILE
    if args.config:
        CONFIG_FILE = BASE_DIR / args.config
        # Derive status file and lockfile from config name for isolation
        config_stem = Path(args.config).stem
        STATUS_FILE = BASE_DIR / "data" / f"orchestrator-status-{config_stem}.json"
        LOCKFILE = BASE_DIR / f".orchestrator-{config_stem}.lock"

    config = load_config()

    if args.command == "status":
        status = load_status()
        print_status(status, datetime.now(ET))
        return

    # Lockfile — skip if another orchestrator is running
    lock_fd = open(LOCKFILE, "w")
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log.info("Another orchestrator instance is running. Skipping.")
        return

    try:
        heartbeat(config, dry_run=args.dry_run)
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        lock_fd.close()


if __name__ == "__main__":
    main()
