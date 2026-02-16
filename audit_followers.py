"""
Follower audit for @tatamispaces.
Reviews accounts we follow — checks if they're active, relevant,
and worth keeping. Produces a report and optionally unfollows.

Usage:
  python audit_followers.py                 # report only
  python audit_followers.py --unfollow      # report + unfollow irrelevant
  python audit_followers.py --dry-run       # show what would happen
"""

import sys
import os
import json
import time
import argparse
from pathlib import Path
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).parent))

from dotenv import load_dotenv
load_dotenv()

from anthropic import Anthropic
from tools.xapi import get_following, unfollow_user, get_user_recent_tweets
from tools.common import setup_logging, load_json, save_json, notify

log = setup_logging("audit")

BASE_DIR = Path(__file__).parent
AUDIT_FILE = BASE_DIR / "data" / "follower-audit.json"

anthropic = Anthropic()
EVAL_MODEL = "claude-opus-4-6"


def evaluate_account(username: str, description: str, recent_tweets: list[dict]) -> dict:
    """Use Claude to evaluate if an account is worth following for @tatamispaces."""
    tweet_texts = "\n".join(
        f"- {t.get('text', '')[:200]}"
        for t in recent_tweets[:5]
    )

    prompt = f"""Evaluate this X account for @tatamispaces (Japanese architecture, interiors, design, craft).

Account: @{username}
Bio: {description or '(no bio)'}

Recent tweets:
{tweet_texts or '(no recent tweets)'}

Score 1-10 for relevance to Japanese architecture/design/interiors/craft:
- 8-10: Core — posts about Japanese architecture, interiors, traditional craft, design
- 6-7: Adjacent — Japan travel with design angle, furniture, ceramics, gardens
- 4-5: Loosely related — general Japan content, some design but not focused
- 1-3: Not relevant — food only, anime, politics, non-Japan, spam, inactive

Return JSON:
{{"score": 7, "keep": true, "reason": "Brief explanation"}}

Set keep=false if score < 6 OR if account appears inactive (no recent tweets)."""

    try:
        response = anthropic.messages.create(
            model=EVAL_MODEL,
            max_tokens=200,
            messages=[{"role": "user", "content": prompt}],
        )
        text = response.content[0].text
        json_start = text.find("{")
        json_end = text.rfind("}") + 1
        if json_start >= 0 and json_end > json_start:
            return json.loads(text[json_start:json_end])
    except Exception as e:
        log.error(f"Failed to evaluate @{username}: {e}")

    return {"score": 5, "keep": True, "reason": "Evaluation failed — keeping by default"}


def load_audit() -> dict:
    return load_json(AUDIT_FILE, default={"audits": [], "last_run": None})


def save_audit(data: dict):
    save_json(AUDIT_FILE, data)


def main():
    parser = argparse.ArgumentParser(description="Audit followed accounts")
    parser.add_argument("--unfollow", action="store_true", help="Actually unfollow low-score accounts")
    parser.add_argument("--dry-run", action="store_true", help="Show what would happen")
    parser.add_argument("--min-score", type=int, default=6, help="Minimum score to keep (default 6)")
    parser.add_argument("--max-unfollow", type=int, default=5, help="Max accounts to unfollow per run (default 5)")
    args = parser.parse_args()

    log.info("Starting follower audit...")

    # Get who we follow
    following = get_following(max_results=500)
    log.info(f"Following {len(following)} accounts")

    if not following:
        log.info("No accounts found or API error")
        return

    # Load previous audit for skip/cache
    audit_data = load_audit()
    prev_audited = {a["username"]: a for a in audit_data.get("audits", [])}

    results = []
    keep_count = 0
    unfollow_count = 0
    skipped_count = 0

    for i, acct in enumerate(following):
        username = acct["username"]

        # Skip if audited in last 7 days
        prev = prev_audited.get(username)
        if prev and prev.get("audited_at"):
            try:
                prev_dt = datetime.fromisoformat(prev["audited_at"])
                days_ago = (datetime.now(timezone.utc) - prev_dt).days
                if days_ago < 7:
                    results.append(prev)
                    if prev.get("keep", True):
                        keep_count += 1
                    else:
                        unfollow_count += 1
                    skipped_count += 1
                    continue
            except Exception:
                pass

        # Rate limit: X API allows 300 user-timeline requests / 15min
        # Be conservative — pause every 10 accounts
        if i > 0 and i % 10 == 0:
            log.info(f"  Progress: {i}/{len(following)} evaluated...")
            time.sleep(2)

        # Get recent tweets
        tweets = get_user_recent_tweets(acct["id"], max_results=5)

        # Evaluate
        evaluation = evaluate_account(username, acct.get("description", ""), tweets)

        result = {
            "username": username,
            "user_id": acct["id"],
            "name": acct.get("name", ""),
            "description": acct.get("description", "")[:100],
            "followers": acct.get("followers_count", 0),
            "tweets_total": acct.get("tweet_count", 0),
            "recent_tweet_count": len(tweets),
            "score": evaluation.get("score", 5),
            "keep": evaluation.get("keep", True),
            "reason": evaluation.get("reason", ""),
            "audited_at": datetime.now(timezone.utc).isoformat(),
        }

        if result["keep"]:
            keep_count += 1
            log.info(f"  KEEP @{username} — score {result['score']}: {result['reason'][:60]}")
        else:
            unfollow_count += 1
            log.info(f"  DROP @{username} — score {result['score']}: {result['reason'][:60]}")

        results.append(result)

    # Save audit results
    audit_data["audits"] = results
    audit_data["last_run"] = datetime.now(timezone.utc).isoformat()
    audit_data["summary"] = {
        "total": len(following),
        "keep": keep_count,
        "unfollow": unfollow_count,
        "cached": skipped_count,
    }
    save_audit(audit_data)

    # Print report
    print()
    print("=" * 60)
    print(f"FOLLOWER AUDIT — @tatamispaces")
    print("=" * 60)
    print(f"Total following:   {len(following)}")
    print(f"Keep:              {keep_count}")
    print(f"Recommend unfollow: {unfollow_count}")
    print(f"Cached (< 7 days): {skipped_count}")
    print()

    # Sort by score ascending (worst first), exclude already-unfollowed
    to_unfollow = sorted(
        [r for r in results if not r["keep"] and not r.get("unfollowed_at")],
        key=lambda r: r["score"],
    )
    if to_unfollow:
        print(f"Accounts to unfollow ({len(to_unfollow)} total):")
        for r in to_unfollow:
            print(f"  @{r['username']} (score {r['score']}) — {r['reason'][:60]}")
        print()

    # Unfollow if requested — capped at --max-unfollow per run
    if args.unfollow and to_unfollow:
        batch = to_unfollow[:args.max_unfollow]
        if args.dry_run:
            print(f"[DRY RUN] Would unfollow {len(batch)} of {len(to_unfollow)} accounts (max {args.max_unfollow}/run)")
            for r in batch:
                print(f"  @{r['username']} (score {r['score']})")
        else:
            unfollowed = 0
            for r in batch:
                success = unfollow_user(r["user_id"])
                if success:
                    unfollowed += 1
                    log.info(f"Unfollowed @{r['username']}")
                    r["unfollowed_at"] = datetime.now(timezone.utc).isoformat()
                else:
                    log.warning(f"Failed to unfollow @{r['username']}")
                time.sleep(2)  # conservative rate limit buffer

            save_audit(audit_data)
            remaining = len(to_unfollow) - unfollowed
            print(f"Unfollowed {unfollowed}/{len(batch)} this run ({remaining} remaining)")
            notify("@tatamispaces audit", f"Unfollowed {unfollowed} accounts ({remaining} remaining), keeping {keep_count}")
    elif to_unfollow and not args.unfollow:
        print(f"Run with --unfollow to remove {len(to_unfollow)} accounts ({args.max_unfollow}/run)")

    log.info(f"Audit saved to {AUDIT_FILE}")


if __name__ == "__main__":
    main()
