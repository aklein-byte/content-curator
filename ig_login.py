"""
One-time interactive login script for instagrapi sessions.

Run this interactively on VPS to handle challenges (2FA / email verification).
Once the session is saved, ig_engage.py will reuse it without needing credentials.

Usage:
    venv/bin/python ig_login.py --niche tatamispaces
    venv/bin/python ig_login.py --niche museumstories
"""

import argparse
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

from instagrapi import Client
from instagrapi.exceptions import ChallengeRequired

BASE_DIR = Path(__file__).parent

EXPECTED_USER_IDS = {
    "tatamispaces": 80137319362,
    "museumstories": 47315951287,
}

CRED_MAP = {
    "tatamispaces": {
        "username_vars": ["IG_USERNAME_TATAMISPACES", "IG_USERNAME"],
        "password_vars": ["IG_PASSWORD_TATAMISPACES", "IG_PASSWORD"],
    },
    "museumstories": {
        "username_vars": ["IG_USERNAME_MUSEUMSTORIES", "IG_USERNAME_MUSEUM"],
        "password_vars": ["IG_PASSWORD_MUSEUMSTORIES", "IG_PASSWORD_MUSEUM"],
    },
}


def get_creds(niche_id: str) -> tuple[str, str]:
    creds = CRED_MAP.get(niche_id, {})
    username_vars = creds.get("username_vars", [f"IG_USERNAME_{niche_id.upper()}"])
    password_vars = creds.get("password_vars", [f"IG_PASSWORD_{niche_id.upper()}"])

    username = None
    for var in username_vars:
        username = os.environ.get(var)
        if username:
            break

    password = None
    for var in password_vars:
        password = os.environ.get(var)
        if password:
            break

    return username or "", password or ""


def challenge_code_handler(username, choice):
    """Called by instagrapi when a challenge code is needed."""
    print(f"\nIG sent a verification code via {choice.name} for @{username}")
    code = input("Enter the 6-digit code: ").strip()
    return code


def main():
    parser = argparse.ArgumentParser(description="Interactive IG login for instagrapi")
    parser.add_argument("--niche", required=True, help="Niche ID (tatamispaces or museumstories)")
    args = parser.parse_args()

    niche_id = args.niche
    session_path = BASE_DIR / "data" / "sessions" / f"ig_session_{niche_id}.json"
    session_path.parent.mkdir(parents=True, exist_ok=True)

    username, password = get_creds(niche_id)
    if not username or not password:
        print(f"ERROR: No credentials found for {niche_id}")
        sys.exit(1)

    print(f"Logging in as @{username} for niche '{niche_id}'...")

    cl = Client()
    cl.delay_range = [3, 7]
    cl.challenge_code_handler = challenge_code_handler

    proxy_url = os.environ.get("RESIDENTIAL_PROXY")
    if proxy_url:
        cl.set_proxy(proxy_url)
        print(f"Using residential proxy")

    try:
        cl.login(username, password)
    except Exception as e:
        print(f"Login failed: {e}")
        sys.exit(1)

    # Validate account
    actual_id = int(cl.user_id)
    expected_id = EXPECTED_USER_IDS.get(niche_id)
    if expected_id and actual_id != expected_id:
        print(f"SAFETY: Wrong account! Expected {expected_id}, got {actual_id}")
        sys.exit(1)

    # Save session
    cl.dump_settings(session_path)
    print(f"\nSession saved to {session_path}")
    print(f"Logged in as user_id={actual_id}")

    # Verify
    info = cl.account_info()
    print(f"Account: @{info.username} ({info.full_name})")
    print("Done. ig_engage.py will now use this session.")


if __name__ == "__main__":
    main()
