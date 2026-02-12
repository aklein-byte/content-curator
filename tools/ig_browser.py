"""
Instagram Playwright browser helpers.

Provides persistent browser context and posting flow for IG via Playwright.
Used by post.py (cross-posting) and ig_engage.py (engagement).
"""

import re
import hashlib
import logging
from pathlib import Path

log = logging.getLogger("ig_browser")

BASE_DIR = Path(__file__).parent.parent
IG_BROWSER_PROFILE = BASE_DIR / "data" / "ig_browser_profile"


async def get_ig_browser(playwright, headless: bool = True) -> "BrowserContext":
    """Launch or reuse a persistent Chromium context with IG cookies."""
    IG_BROWSER_PROFILE.mkdir(parents=True, exist_ok=True)
    context = await playwright.chromium.launch_persistent_context(
        str(IG_BROWSER_PROFILE),
        headless=headless,
        viewport={"width": 430, "height": 932},
        user_agent=(
            "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) "
            "Version/17.0 Mobile/15E148 Safari/604.1"
        ),
        is_mobile=True,
        has_touch=True,
    )
    return context


async def post_to_ig_browser(page, image_paths: list[str], caption: str) -> bool:
    """
    Post image(s) to Instagram via Playwright browser automation.

    Flow: New post -> file input -> select Original aspect ratio -> Next -> Next -> caption -> Share

    Falls back to single image if carousel upload fails.
    """
    try:
        # Click new post button (+ icon)
        new_post_btn = page.locator('svg[aria-label="New post"]').first
        if await new_post_btn.count() == 0:
            # Try alternate selector
            new_post_btn = page.locator('[aria-label="New post"]').first
        if await new_post_btn.count() == 0:
            log.error("Could not find New Post button")
            return False

        await new_post_btn.click()
        await page.wait_for_timeout(3000)

        # Find file input (hidden, need state="attached")
        file_input = page.locator('input[type="file"]').first
        await file_input.wait_for(state="attached", timeout=10000)

        # Try carousel (all files at once)
        try:
            await file_input.set_input_files(image_paths)
            await page.wait_for_timeout(2000)
        except Exception as e:
            log.warning(f"Carousel upload failed ({e}), falling back to single image")
            await file_input.set_input_files(image_paths[0])
            await page.wait_for_timeout(2000)

        # Select Original aspect ratio (critical — IG defaults to square crop)
        try:
            # Click the resize/crop icon
            resize_btn = page.locator('svg[aria-label="Select crop"]').first
            if await resize_btn.count() == 0:
                resize_btn = page.locator('svg[aria-label="Crop"]').first
            if await resize_btn.count() > 0:
                await resize_btn.click()
                await page.wait_for_timeout(1000)

                # Select "Original"
                original_btn = page.locator('svg[aria-label="Photo outline icon"]').first
                if await original_btn.count() == 0:
                    original_btn = page.locator('svg[aria-label="Original"]').first
                if await original_btn.count() > 0:
                    await original_btn.click()
                    await page.wait_for_timeout(500)
                else:
                    log.warning("Could not find Original aspect ratio button")
            else:
                log.warning("Could not find crop/resize button")
        except Exception as e:
            log.warning(f"Aspect ratio selection failed: {e}")

        # Click Next (crop step)
        next_btn = page.locator('div[role="button"]:has-text("Next")').first
        if await next_btn.count() > 0:
            await next_btn.click()
            await page.wait_for_timeout(2000)

        # Click Next (filters step)
        next_btn = page.locator('div[role="button"]:has-text("Next")').first
        if await next_btn.count() > 0:
            await next_btn.click()
            await page.wait_for_timeout(2000)

        # Enter caption
        caption_area = page.locator('textarea[aria-label="Write a caption..."]').first
        if await caption_area.count() == 0:
            caption_area = page.locator('div[aria-label="Write a caption..."]').first
        if await caption_area.count() > 0:
            await caption_area.click()
            await page.wait_for_timeout(500)
            await caption_area.fill(caption)
            await page.wait_for_timeout(500)

        # Click Share
        share_btn = page.locator('div[role="button"]:has-text("Share")').first
        if await share_btn.count() > 0:
            await share_btn.click()
            await page.wait_for_timeout(5000)
            log.info("IG post shared successfully")
            return True
        else:
            log.error("Could not find Share button")
            return False

    except Exception as e:
        log.error(f"post_to_ig_browser failed: {e}")
        return False


def adapt_caption_for_ig(text: str, niche: dict) -> str:
    """Adapt X caption for Instagram (convert Twitter credits, add hashtags)."""
    ig_text = re.sub(r'📷\s*@(\w+)', r'📷 \1 on X', text)
    hashtags = niche.get("hashtags", [])
    if hashtags:
        ig_text += "\n\n" + " ".join(hashtags[:10])
    return ig_text


def ensure_jpeg(img_path: Path) -> Path:
    """Convert to JPEG if needed."""
    from PIL import Image

    if img_path.suffix.lower() in (".jpg", ".jpeg"):
        return img_path

    jpeg_path = img_path.with_suffix(".jpg")
    if jpeg_path.exists():
        return jpeg_path

    img = Image.open(img_path)
    if img.mode in ("RGBA", "P"):
        img = img.convert("RGB")
    img.save(jpeg_path, "JPEG", quality=95)
    return jpeg_path


async def download_image(url: str, save_dir: Path) -> Path | None:
    """Download an image from a URL."""
    import httpx

    # Upgrade Twitter image URLs to original resolution
    if "pbs.twimg.com" in url and "name=orig" not in url:
        base = url.split("?")[0]
        if base.endswith((".jpg", ".jpeg", ".png", ".webp")):
            ext_str = base.rsplit(".", 1)[-1]
            base = base.rsplit(".", 1)[0]
            url = f"{base}?format={ext_str}&name=orig"

    save_dir = Path(save_dir)
    save_dir.mkdir(parents=True, exist_ok=True)
    ext = ".jpg"
    if ".png" in url or "format=png" in url:
        ext = ".png"
    filename = hashlib.md5(url.encode()).hexdigest() + ext
    save_path = save_dir / filename

    if save_path.exists() and save_path.stat().st_size > 10_000:
        return save_path

    try:
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as http:
            resp = await http.get(url, headers={
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"
            })
            if resp.status_code == 200 and len(resp.content) > 10_000:
                save_path.write_bytes(resp.content)
                return save_path
    except Exception as e:
        log.error(f"Failed to download {url}: {e}")
    return None
