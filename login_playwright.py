"""
Login to X using Playwright (real browser).
Opens X login page — you complete the login manually.
Once logged in, cookies are saved for twikit to reuse.

Usage: python login_playwright.py
"""

import sys
import os
import json
import asyncio
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from dotenv import load_dotenv
load_dotenv()


async def main():
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        print("Run: pip install playwright && playwright install chromium")
        return

    cookies_dir = Path(os.environ.get("COOKIES_DIR", "data/cookies"))
    cookies_dir.mkdir(parents=True, exist_ok=True)
    cookies_file = cookies_dir / "tatamispaces_cookies.json"

    print("Opening X login page...")
    print("Log in manually in the browser window that opens.")
    print("Once you see your home feed, cookies will be saved automatically.\n")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        page = await context.new_page()

        await page.goto("https://x.com/i/flow/login", wait_until="domcontentloaded", timeout=60000)

        # Wait until the user logs in and reaches the home page
        print("Waiting for login to complete...")
        try:
            await page.wait_for_url("**/home**", timeout=300000)  # 5 minute timeout
        except Exception:
            # Check if we're on any x.com page (user might have navigated elsewhere)
            if "x.com" in page.url and "login" not in page.url:
                pass
            else:
                print(f"Login may not have completed. Current URL: {page.url}")
                print("Close the browser when done.")
                await page.wait_for_event("close", timeout=300000)
                await browser.close()
                return

        print("\nLogin detected! Saving cookies...")

        # Get all cookies
        cookies = await context.cookies()
        x_cookies = [c for c in cookies if "x.com" in c.get("domain", "") or "twitter.com" in c.get("domain", "")]

        # Save in twikit-compatible format (simple name:value dict)
        twikit_cookies = {}
        for c in x_cookies:
            twikit_cookies[c["name"]] = c["value"]

        cookies_file.write_text(json.dumps(twikit_cookies, indent=2))

        has_auth = "auth_token" in twikit_cookies
        has_ct0 = "ct0" in twikit_cookies

        print(f"Saved {len(twikit_cookies)} cookies to {cookies_file}")
        print(f"auth_token: {'found' if has_auth else 'MISSING'}")
        print(f"ct0: {'found' if has_ct0 else 'MISSING'}")

        if has_auth and has_ct0:
            print("\nAll scripts should now work with cached cookies.")
        else:
            print("\nWarning: missing critical cookies.")

        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
