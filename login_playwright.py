"""
Login to X using Playwright (real browser).
Handles verification challenges, CAPTCHAs, 2FA — anything X throws.
Exports cookies for twikit to reuse.

Usage: python login_playwright.py [--headless]

Without --headless: opens a visible browser window so you can handle
any challenges manually. Once logged in, cookies are saved automatically.
"""

import sys
import os
import json
import asyncio
import argparse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from dotenv import load_dotenv
load_dotenv()


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--headless", action="store_true", help="Run without visible browser")
    args = parser.parse_args()

    try:
        from playwright.async_api import async_playwright
    except ImportError:
        print("Playwright not installed. Run:")
        print("  pip install playwright")
        print("  playwright install chromium")
        return

    username = os.environ.get("X_USERNAME", "")
    email = os.environ.get("X_EMAIL", "")
    password = os.environ.get("X_PASSWORD", "")

    if not all([username, email, password]):
        print("Missing X_USERNAME, X_EMAIL, or X_PASSWORD in .env")
        return

    cookies_dir = Path(os.environ.get("COOKIES_DIR", "data/cookies"))
    cookies_dir.mkdir(parents=True, exist_ok=True)
    cookies_file = cookies_dir / "tatamispaces_cookies.json"
    pw_cookies_file = cookies_dir / "tatamispaces_pw_state.json"

    print(f"Logging in as @{username}...")
    print(f"Browser: {'headless' if args.headless else 'visible'}")
    print()

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=args.headless)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        page = await context.new_page()

        # Go to X login
        await page.goto("https://x.com/i/flow/login", wait_until="networkidle")
        await asyncio.sleep(2)

        # Enter username
        print("Entering username...")
        username_input = page.locator('input[autocomplete="username"]')
        await username_input.fill(username)
        await page.locator('text=Next').click()
        await asyncio.sleep(2)

        # Check if X asks for email verification (unusual login challenge)
        page_text = await page.content()
        if "phone" in page_text.lower() or "email" in page_text.lower():
            # Might ask for email to verify identity
            try:
                email_input = page.locator('input[data-testid="ocfEnterTextTextInput"]')
                if await email_input.is_visible(timeout=3000):
                    print("X is asking for email verification...")
                    await email_input.fill(email)
                    await page.locator('text=Next').click()
                    await asyncio.sleep(2)
            except Exception:
                pass

        # Enter password
        print("Entering password...")
        try:
            password_input = page.locator('input[type="password"]')
            await password_input.wait_for(timeout=10000)
            await password_input.fill(password)
            await page.locator('text=Log in').click()
        except Exception:
            # Password field might be on a different screen
            print("Looking for password field...")
            await asyncio.sleep(2)
            password_input = page.locator('input[type="password"]')
            await password_input.fill(password)
            await page.locator('text=Log in').click()

        await asyncio.sleep(3)

        # Check if we need to handle additional challenges
        current_url = page.url
        if "challenge" in current_url or "verify" in current_url:
            if args.headless:
                print("\nX is showing a challenge that requires manual interaction.")
                print("Re-run without --headless to handle it in a visible browser.")
                await browser.close()
                return
            else:
                print("\nX is showing a verification challenge.")
                print("Please complete it in the browser window.")
                print("Waiting for you to finish...")

                # Wait until we're on the home page or bookmarks
                try:
                    await page.wait_for_url("**/home**", timeout=120000)
                except Exception:
                    pass

        # Check if login succeeded
        await asyncio.sleep(2)
        current_url = page.url
        if "home" in current_url or "x.com" in current_url:
            print("\nLogin successful!")

            # Get cookies
            cookies = await context.cookies()
            x_cookies = [c for c in cookies if "x.com" in c.get("domain", "") or "twitter.com" in c.get("domain", "")]

            # Save in Playwright format (for future Playwright use)
            await context.storage_state(path=str(pw_cookies_file))
            print(f"Playwright state saved: {pw_cookies_file}")

            # Convert to twikit format (simple dict of name: value)
            twikit_cookies = {}
            for c in x_cookies:
                twikit_cookies[c["name"]] = c["value"]

            # Save in twikit-compatible format
            cookies_file.write_text(json.dumps(twikit_cookies, indent=2))
            print(f"Twikit cookies saved: {cookies_file}")

            # Show key cookies
            has_auth = "auth_token" in twikit_cookies
            has_ct0 = "ct0" in twikit_cookies
            print(f"\nauth_token: {'found' if has_auth else 'MISSING'}")
            print(f"ct0: {'found' if has_ct0 else 'MISSING'}")

            if has_auth and has_ct0:
                print("\nAll scripts should now work with cached cookies.")
            else:
                print("\nWarning: missing critical cookies. Login may not have fully completed.")
        else:
            print(f"\nLogin may have failed. Current URL: {current_url}")
            if not args.headless:
                print("Check the browser window for any issues.")
                input("Press Enter when done...")

        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
