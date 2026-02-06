"""
One-time interactive login to X via twikit.
Run this in your terminal. If X sends a verification code to your email,
it will prompt you to enter it. After success, cookies are saved and
all other scripts (engage.py, post.py, etc.) will use the cached session.

Usage: python login_once.py
"""

import sys
import os
import asyncio
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from dotenv import load_dotenv
load_dotenv()

from twikit import Client


async def main():
    username = os.environ.get("X_USERNAME", "")
    email = os.environ.get("X_EMAIL", "")
    password = os.environ.get("X_PASSWORD", "")

    if not all([username, email, password]):
        print("Missing X_USERNAME, X_EMAIL, or X_PASSWORD in .env")
        return

    cookies_dir = Path(os.environ.get("COOKIES_DIR", "data/cookies"))
    cookies_dir.mkdir(parents=True, exist_ok=True)
    cookies_file = str(cookies_dir / "tatamispaces_cookies.json")

    print(f"Logging in as @{username}...")
    print(f"Email: {email}")
    print()

    client = Client("en-US")

    try:
        await client.login(
            auth_info_1=username,
            auth_info_2=email,
            password=password,
        )
    except Exception as e:
        error_msg = str(e)
        if "confirmation" in error_msg.lower() or "challenge" in error_msg.lower():
            print("X is asking for verification.")
            print("Check your email for a code from X.")
            code = input("Enter the code: ").strip()
            # Some twikit versions handle this differently
            try:
                await client.login(
                    auth_info_1=username,
                    auth_info_2=email,
                    password=password,
                    totp_secret=code,
                )
            except Exception as e2:
                print(f"Login failed after code: {e2}")
                return
        else:
            print(f"Login failed: {e}")
            return

    # Save cookies
    client.save_cookies(cookies_file)
    print()
    print(f"Login successful! Cookies saved to: {cookies_file}")
    print("All scripts (engage.py, post.py, etc.) will now use this session.")
    print()

    # Quick verify
    me = await client.user()
    print(f"Logged in as: @{me.screen_name} ({me.name})")
    print(f"Followers: {me.followers_count}")


if __name__ == "__main__":
    asyncio.run(main())
