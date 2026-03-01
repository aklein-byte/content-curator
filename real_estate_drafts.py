#!/usr/bin/env python3
"""
Real Estate Draft Generator for @tatamispaces.

Uses Firecrawl to search Japanese real estate listing sites for properties
with great photos. Creates draft posts for owner approval via dashboard.

Sources: boutique JP real estate sites, architect portfolios, kominka/machiya listings.
Targets: design-forward properties AND classic traditional homes, with prices.

Usage: python real_estate_drafts.py [--niche tatamispaces] [--dry-run] [--max-drafts 5]
"""

import sys
import os
import re
import json
import logging
import argparse
import random
from pathlib import Path
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).parent))

from dotenv import load_dotenv
load_dotenv()

from tools.common import load_json, save_json, setup_logging, load_config, get_model
from config.niches import get_niche

log = setup_logging("real_estate")

BASE_DIR = Path(__file__).parent
POSTS_FILE = Path(os.environ.get("POSTS_FILE", str(BASE_DIR / "posts.json")))

MAX_RE_QUEUE = 10  # Don't generate more if this many in queue

# Lazy-loaded NIMA model (loaded once, reused across listings)
_nima_model = None

def _get_nima():
    global _nima_model
    if _nima_model is None:
        import pyiqa
        log.info("Loading NIMA aesthetic model...")
        _nima_model = pyiqa.create_metric("nima", device="cpu")
    return _nima_model

# Known index/listing pages to crawl for individual property URLs
# Prioritize sites that list PRICES and have decent photos
LISTING_INDEXES = [
    # Japan Kominka — has prices (¥32M-250M), 35+ photos, Kyoto machiya
    "https://japan-kominka.com/",
    # Hachise — Kyoto machiya, some have prices
    "https://www.hachise.com/buy/list.html",
    # Uchi Japan — resort/vacation, most have prices, 2048px images
    "https://uchijapan.com/properties?search[sortBy]=date_desc",
    # KORYOYA — great photos but rarely lists prices, use as fallback
    "https://www.koryoya.com/kominka/index.html",
]

# Fallback search queries for discovery (individual listings, not index pages)
SEARCH_QUERIES = [
    "site:koryoya.com kominka for sale",
    "site:japan-kominka.com property for sale kyoto",
    "site:hachise.com machiya for sale",
    "kominka for sale japan architect renovated listing",
    "machiya for sale kyoto traditional interior listing price",
    "japanese architect house for sale photos price",
]


def _get_firecrawl():
    """Initialize Firecrawl client."""
    from firecrawl import FirecrawlApp
    return FirecrawlApp()


def _crawl_index_for_links(app, index_url: str) -> list[str]:
    """Scrape a listing index page and extract individual property URLs."""
    try:
        result = app.scrape(index_url, formats=["markdown", "links"])
        links = []
        # Try links attribute first
        if hasattr(result, "links") and result.links:
            links = result.links
        # Also extract from markdown
        md = result.markdown if hasattr(result, "markdown") else ""
        import re as _re
        md_links = _re.findall(r'\[.*?\]\((https?://[^\)]+)\)', md)
        links.extend(md_links)
        # Also find bare URLs
        bare_links = _re.findall(r'(?:href="|\()(https?://[^"\)\s]+)', md)
        links.extend(bare_links)

        from urllib.parse import urlparse, urljoin
        base_domain = urlparse(index_url).netloc
        property_urls = []
        seen = set()

        for link in links:
            # Make absolute
            if link.startswith("/"):
                link = urljoin(index_url, link)

            # Only same-domain links
            if urlparse(link).netloc != base_domain:
                continue

            # Skip non-property pages and asset files
            lower = link.lower()
            if lower.endswith(('.svg', '.png', '.jpg', '.jpeg', '.webp', '.gif',
                               '.css', '.js', '.pdf', '.ico', '.woff', '.woff2')):
                continue
            if any(skip in lower for skip in [
                "/about", "/contact", "/blog", "/faq", "/team", "/services",
                "/list-your", "/login", "/register", "/privacy", "/terms",
                "/sell", "/rent", "/agents", "/assets/", "/img/", "/images/",
                "/common/", "/static/", "/css/", "/js/",
                "/category", "/tag", "/page/", "#"
            ]):
                continue

            # Must look like an individual listing (has an ID, slug, or number in path)
            parsed = urlparse(link)
            path = parsed.path
            query = parsed.query.lower()
            # Skip pagination and sort/filter pages
            if any(param in query for param in ["page=", "order=", "sort=", "agent_id="]):
                if "view/" not in path:  # Keep actual property views with query params
                    continue
            # Heuristic: property pages usually have deeper paths or numeric IDs
            parts = [p for p in path.split("/") if p]
            if len(parts) < 2:
                continue  # Too shallow, probably a section page

            if link in seen:
                continue
            seen.add(link)
            property_urls.append(link)

        # Cap per site: fewer for priceless sites, more for priced ones
        from urllib.parse import urlparse as _up
        domain = _up(index_url).netloc
        if "koryoya" in domain:
            cap = 3  # Rarely has prices, just a few for variety
        elif "hachise" in domain:
            cap = 5  # Sometimes has prices
        else:
            cap = 15  # japan-kominka, uchijapan — usually have prices
        return property_urls[:cap]
    except Exception as e:
        log.warning(f"  Failed to crawl index {index_url}: {e}")
        return []


def load_posts() -> dict:
    return load_json(POSTS_FILE, default={"posts": []})


def save_posts(data: dict):
    save_json(POSTS_FILE, data)


def _get_existing_re_urls(posts_data: dict) -> set:
    """Get source URLs of existing real estate posts (any status)."""
    urls = set()
    for p in posts_data.get("posts", []):
        if p.get("type") == "real-estate":
            url = p.get("source_url", "")
            if url:
                urls.add(url)
    return urls


def _get_next_id(posts_data: dict) -> int:
    max_id = 0
    for p in posts_data.get("posts", []):
        pid = p.get("id", 0)
        if isinstance(pid, int) and pid > max_id:
            max_id = pid
    return max_id + 1


def _get_queued_re_summaries(posts_data: dict) -> str:
    """Get summaries of RE posts in queue for similarity check."""
    summaries = []
    for p in posts_data.get("posts", []):
        if p.get("type") == "real-estate" and p.get("status") in ("draft", "approved"):
            summaries.append(f"- {p.get('text', '')[:80]}")
    return "\n".join(summaries) if summaries else "None"


def _extract_images_from_markdown(md: str) -> list[str]:
    """Extract image URLs from markdown content, upgrading to full-res and filtering junk."""
    pattern = r'!\[.*?\]\((https?://[^\)]+)\)'
    urls = re.findall(pattern, md)
    # Also grab bare image URLs not in markdown syntax
    bare_pattern = r'(?:src="|href=")(https?://[^"]+\.(?:jpg|jpeg|png|webp)[^"]*)'
    urls.extend(re.findall(bare_pattern, md, re.IGNORECASE))

    images = []
    seen_base = set()

    for url in urls:
        lower = url.lower()

        # --- SKIP junk ---
        # Site-wide assets (logos, icons, banners, feature images, OG images)
        if any(skip in lower for skip in [
            "logo", "icon", "favicon", "avatar", "badge", "button",
            "1x1", "pixel", "spacer", "blank", "placeholder",
            "ogimg", "og_image", "og-image", "main_img",
            "feature/", "/banner", "/header", "social-share",
            "realestatejapan.png", "sprite", "arrow", "close",
            "search", "map-pin", "marker",
            # realestate.co.jp site assets (not property photos)
            "/sora-cn/", "/sora-en/", "/loan-cn/", "/loan-en/",
            "/qrt/", "/cl/", "/store-cn/", "/store-en/",
        ]):
            continue
        # Non-photo formats
        if any(ext in lower for ext in [".svg", ".gif", ".ico", ".webm", ".mp4"]):
            continue
        # Tiny thumbnail indicators in filename
        if re.search(r'_w(?:80|100|120|150|160|200)_h', lower):
            continue
        if re.search(r'list(?:100|200|300)\b', lower):
            continue

        # --- UPGRADE to full-res ---
        full = url

        # realestate.co.jp: _w900_h600 versions are already good quality (900x600)
        # Just keep them — don't try to find a "full-res" version that doesn't exist
        # Only the _w100_h100_c thumbnails get filtered by the skip rules above

        # Hachise: list600.jpg -> photo URLs are at /img/photo_XX.jpg
        # Can't reliably upgrade, but at least keep list600 (it's 600px wide, ok for preview)

        # Koryoya: strip list600 pattern
        full = re.sub(r'/list\d+\.(jpg|jpeg|png|webp)', '/main.\1', full)

        # Generic: strip resize query params
        full = re.sub(r'[?&](w|width|h|height|resize|size|fit|crop|quality|q)=[^&]*', '', full)
        # Strip dimension suffixes like -300x200.jpg
        full = re.sub(r'-\d+x\d+(?=\.(?:jpg|jpeg|png|webp))', '', full)
        # Cloudfront crop patterns
        full = re.sub(r'/_\d+x\d+_crop[^/]*/', '/', full)
        full = full.rstrip('?&')

        # --- DEDUPLICATE ---
        # Normalize: strip all size indicators to find the "base" image
        base = re.sub(r'[?#].*$', '', full)
        # Strip path components that are just sizes
        base = re.sub(r'/_?w?\d+_?h?\d+[^/]*(?=\.)', '', base)
        base_name = base.split('/')[-1].lower()
        # Also match by path without filename (catches same image different sizes)
        base_path = '/'.join(base.split('/')[:-1])

        dedup_key = base_name
        if dedup_key in seen_base:
            continue
        seen_base.add(dedup_key)
        images.append(full)

    return images


def _score_images_with_vision(images: list[str], listing_url: str) -> list[str]:
    """Use Claude vision to score images and return only good property photos."""
    if not images:
        return []
    from anthropic import Anthropic
    import httpx

    _cfg = load_config().get("models", {})
    model = get_model("vision")
    client = Anthropic()

    # Download images and build vision content
    content_blocks = []
    valid_images = []
    for url in images[:8]:  # Max 8 to keep cost down
        try:
            resp = httpx.get(url, timeout=10, follow_redirects=True)
            if resp.status_code != 200:
                continue
            ct = resp.headers.get("content-type", "")
            if "image" not in ct:
                continue
            # Skip tiny images (< 5KB likely icons/placeholders)
            if len(resp.content) < 5000:
                continue
            import base64
            media_type = ct.split(";")[0].strip()
            if media_type not in ("image/jpeg", "image/png", "image/webp", "image/gif"):
                media_type = "image/jpeg"
            # Resize if too large for API (>4.5MB)
            img_bytes = resp.content
            if len(img_bytes) > 4_500_000:
                from PIL import Image as _PILImg
                import io as _io2
                pil = _PILImg.open(_io2.BytesIO(img_bytes))
                # Scale down to max 1920px
                pil.thumbnail((1920, 1920))
                buf = _io2.BytesIO()
                pil.save(buf, format="JPEG", quality=85)
                img_bytes = buf.getvalue()
                media_type = "image/jpeg"
            b64 = base64.b64encode(img_bytes).decode()
            content_blocks.append({
                "type": "image",
                "source": {"type": "base64", "media_type": media_type, "data": b64}
            })
            content_blocks.append({
                "type": "text",
                "text": f"Image {len(valid_images)}: {url}"
            })
            valid_images.append(url)
        except Exception:
            continue

    if not valid_images:
        return images[:4]  # Fallback: return originals

    content_blocks.append({
        "type": "text",
        "text": f"""Score each image for a real estate post on @tatamispaces (Japanese architecture account).

For each image, reply GOOD or BAD:
- GOOD = actual property photo showing interior, exterior, garden, or architectural detail. High enough quality to post.
- BAD = site logo, banner, map, floor plan, agent photo, icon, blurry, too small, generic stock, or not a property photo.

Reply with ONLY a numbered list like:
0: GOOD
1: BAD
2: GOOD
..."""
    })

    try:
        response = client.messages.create(
            model=model,
            max_tokens=200,
            messages=[{"role": "user", "content": content_blocks}],
        )
        text = response.content[0].text.strip()
        good_images = []
        for line in text.split("\n"):
            line = line.strip()
            if "GOOD" in line.upper():
                try:
                    idx = int(line.split(":")[0].strip())
                    if 0 <= idx < len(valid_images):
                        good_images.append(valid_images[idx])
                except (ValueError, IndexError):
                    continue
        return good_images if good_images else valid_images[:2]
    except Exception as e:
        log.warning(f"Vision scoring failed: {e}")
        return valid_images[:4]




def _score_image_aesthetics(images: list[str], listing_url: str) -> list[dict]:
    """Score images with NIMA (aesthetic quality) locally. Returns scored list sorted by quality."""
    import httpx
    from PIL import Image as PILImage
    import io as _io

    try:
        import torch
        import torchvision.transforms.functional as TF
    except ImportError:
        log.warning("torch not available, skipping aesthetic scoring")
        return [{"url": u, "nima": 5.0} for u in images]

    nima = _get_nima()
    scored = []

    for url in images[:8]:
        try:
            r = httpx.get(url, timeout=10, follow_redirects=True,
                         headers={"Referer": listing_url, "User-Agent": "Mozilla/5.0"})
            if r.status_code != 200 or "image" not in r.headers.get("content-type", ""):
                continue
            if len(r.content) < 3000:
                continue
            pil = PILImage.open(_io.BytesIO(r.content)).convert("RGB")
            w, h = pil.size
            if w < 400 or h < 300:
                continue
            # Score with NIMA
            resized = pil.resize((384, 384), PILImage.LANCZOS)
            tensor = TF.to_tensor(resized).unsqueeze(0)
            with torch.no_grad():
                nima_score = nima(tensor).item()
            scored.append({"url": url, "nima": round(nima_score, 2), "w": w, "h": h})
        except Exception as e:
            log.debug(f"  Aesthetic score failed for {url}: {e}")
            continue

    # Sort by NIMA score (higher = more aesthetic)
    scored.sort(key=lambda x: x["nima"], reverse=True)
    return scored


def _scrape_listing(app, url: str) -> dict | None:
    """Scrape a single listing page for text + images. Fast, Firecrawl-only."""
    try:
        result = app.scrape(url, formats=["markdown"])
        md = result.markdown if hasattr(result, "markdown") else ""
        meta = result.metadata if hasattr(result, "metadata") else {}

        title = meta.title if hasattr(meta, "title") else ""
        description = meta.description if hasattr(meta, "description") else ""
        og_image = meta.og_image if hasattr(meta, "og_image") else ""

        images = _extract_images_from_markdown(md)
        if og_image and og_image not in images:
            og_lower = og_image.lower()
            if not any(skip in og_lower for skip in ["logo", "ogimg", "main_img", "realestatejapan", "og-image"]):
                images.insert(0, og_image)

        # Step 1: NIMA aesthetic scoring (local, free, fast) — filter ugly images
        if images:
            log.info(f"  NIMA-scoring {len(images)} images...")
            scored = _score_image_aesthetics(images, url)
            # Keep images with decent aesthetics (NIMA > 4.5)
            aesthetic = [s for s in scored if s["nima"] >= 4.5]
            if not aesthetic and scored:
                aesthetic = scored[:4]  # Fallback: keep top 4
            avg_nima = sum(s["nima"] for s in aesthetic) / len(aesthetic) if aesthetic else 0
            log.info(f"  {len(aesthetic)} aesthetic images (avg NIMA: {avg_nima:.1f})")
            aesthetic_urls = [s["url"] for s in aesthetic[:8]]
        else:
            aesthetic_urls = []
            avg_nima = 0

        # Step 2: Vision-score top aesthetics to filter junk (maps, logos, plans)
        if aesthetic_urls:
            log.info(f"  Vision-filtering {len(aesthetic_urls)} images with Opus...")
            final_images = _score_images_with_vision(aesthetic_urls, url)
            log.info(f"  {len(final_images)} final images")
        else:
            final_images = []

        return {
            "url": url,
            "title": title,
            "description": description,
            "markdown": md[:6000],
            "images": final_images,
            "og_image": og_image,
            "avg_nima": round(avg_nima, 2),
        }
    except Exception as e:
        log.warning(f"  Scrape failed for {url}: {e}")
        return None


def _evaluate_listing(listing: dict, niche_id: str, queue_summaries: str) -> dict | None:
    """Use Claude to evaluate if a listing is worth posting and draft the caption."""
    from anthropic import Anthropic

    voice_path = BASE_DIR / "config" / "voice.md"
    voice_guide = voice_path.read_text() if voice_path.exists() else ""

    _cfg = load_config().get("models", {})
    model = get_model("reply_drafter")
    client = Anthropic()

    prompt = f"""You're evaluating a Japanese real estate listing for @tatamispaces to post about.

## The listing
URL: {listing['url']}
Title: {listing['title']}
Description: {(listing.get('description') or '')[:200]}
Number of photos: {len(listing['images'])}
Photo quality score: {listing.get('avg_nima', 0):.1f}/10 (NIMA aesthetic score, 5+ is good, 6+ is great)

Content excerpt:
{listing['markdown'][:1500]}

## What we're looking for
Properties in Japan worth posting about. Two types we love:
1. **Design-forward**: Architect-designed, modern renovations, interesting spatial ideas
2. **Classic traditional**: Kominka, machiya, ryokan — aged wood, tatami, real character

POST if the listing has:
- Located in Japan
- Has good photos (NIMA score 5+ means decent, 6+ means great)
- Something interesting to say about it: unusual size, wild price, cool history, famous architect, unique feature
- A price is a bonus, not a requirement

ONLY SKIP if:
- It's a generic modern apartment/condo with zero character
- It's not in Japan
- It's a search results page, not an actual listing
- Nearly identical to something already in our queue
- It's vacant land with no building

If no price is shown, use "Price upon request" — DON'T skip just because there's no price. A price is nice to have — it makes the post more engaging. But great photos and an interesting story are enough. Don't skip just because there's no price.

LEAN TOWARD POSTING. We curate photos manually later — your job is to find listings with a good story and write a great caption. If in doubt, POST.

## Already in our queue (avoid similar)
{queue_summaries}

## Voice guide (follow strictly)
{voice_guide[:1000]}

## Caption style (study these examples from @TimurNegru)
- "Someone is selling their mountain in Andalucía (Spain). 280 hectares. For €1.5M."
- "I think I just found the best living art value in Italy. €335k ($390k) for a renovated 1600s palazzo in Marche."
- "360m2 (3,875 sq ft) across 3 floors (the size of this place for the price is wild)"
- "Serra da Estrela sits at 2,000m altitude and it's the country's only ski resort. 39 chalets were built, 32 already sold and 7 are left at €250k."

The pattern: lead with a hook or discovery ("someone is selling..." / "just found..."), then hit them with the price and one wild detail. Short sentences. Personal reaction when earned ("the size for the price is wild"). No marketing speak. Write like you're texting a friend about something you found.

## Task
1. Decide: POST or SKIP
2. If POST, write a caption (2-4 sentences, can be a thread-starter up to 500 chars) that:
   - Opens with a hook — what you found and why it caught your eye
   - States the price in USD upfront (convert from yen at ~150 JPY/USD)
   - Drops one concrete spec: sqm/sqft, year built, rooms, land size, architect name
   - Ends with the detail that sticks — the thing that makes someone go "wait really?"
   - DO NOT credit the source in the caption (we link it separately)
   - DO NOT use: "nestled", "stunning", "gem", "haven", "retreat", "harmonious", "blend of", "meets", "step into"
   - Write like you're telling a friend about a listing you just found, not writing ad copy
3. Pick a category

Respond in exactly this format:
VERDICT: POST or SKIP
REASON: [one sentence]
CATEGORY: [one of: residential, historic-house, modern-architecture, ryokan, adaptive-reuse, other]
PRICE: [price in USD, e.g. "$230K" or "$1.2M". Convert from yen at ~150 JPY/USD. Write "Price upon request" if no price found — this is normal for Japan]
LOCATION: [city/prefecture]
TEXT: [caption, 2-4 sentences, up to 500 chars]"""

    try:
        response = client.messages.create(
            model=model,
            max_tokens=400,
            messages=[{"role": "user", "content": prompt}],
        )

        text = response.content[0].text.strip()

        verdict = ""
        reason = ""
        category = "residential"
        price = ""
        location = ""
        caption = ""

        for line in text.split("\n"):
            line = line.strip()
            if line.startswith("VERDICT:"):
                verdict = line.replace("VERDICT:", "").strip().upper()
            elif line.startswith("REASON:"):
                reason = line.replace("REASON:", "").strip()
            elif line.startswith("CATEGORY:"):
                category = line.replace("CATEGORY:", "").strip()
            elif line.startswith("PRICE:"):
                price = line.replace("PRICE:", "").strip()
            elif line.startswith("LOCATION:"):
                location = line.replace("LOCATION:", "").strip()
            elif line.startswith("TEXT:"):
                caption = line.replace("TEXT:", "").strip()

        if verdict != "POST" or not caption:
            log.info(f"  SKIP: {reason}")
            return None

        log.info(f"  POST: {reason}")
        return {
            "text": caption[:280],
            "category": category,
            "price": price,
            "location": location,
            "reason": reason,
        }

    except Exception as e:
        log.error(f"  Claude evaluation failed: {e}")
        return None


def find_listings(niche_id: str, max_drafts: int = 5, dry_run: bool = False):
    """Search for Japanese real estate listings and create draft posts."""
    posts_data = load_posts()

    # Queue guard
    queued = sum(
        1 for p in posts_data.get("posts", [])
        if p.get("type") == "real-estate" and p.get("status") in ("draft", "approved")
    )
    if queued >= MAX_RE_QUEUE:
        log.info(f"Already {queued} RE listings in queue. Skipping.")
        return 0

    existing_urls = _get_existing_re_urls(posts_data)
    queue_summaries = _get_queued_re_summaries(posts_data)

    log.info(f"Existing RE URLs: {len(existing_urls)}, queued: {queued}")
    log.info(f"Looking for up to {max_drafts} new real estate drafts")

    app = _get_firecrawl()

    candidates = []
    seen_urls = set()

    # Stage 1: Crawl known listing index pages for individual property URLs
    indexes = LISTING_INDEXES  # Crawl all indexes — fast with Firecrawl
    for index_url in indexes:
        log.info(f"Crawling index: {index_url[:60]}...")
        property_urls = _crawl_index_for_links(app, index_url)
        log.info(f"  Found {len(property_urls)} property links")

        for url in property_urls:
            if url in existing_urls or url in seen_urls:
                continue
            seen_urls.add(url)
            candidates.append({"url": url, "title": "", "description": ""})

    # Stage 2: Fallback search queries for discovery
    if len(candidates) < max_drafts * 2:
        queries = random.sample(SEARCH_QUERIES, min(2, len(SEARCH_QUERIES)))
        for query in queries:
            log.info(f"Searching: {query[:60]}...")
            try:
                results = app.search(query, limit=5)
                web_results = results.web or []
            except Exception as e:
                log.warning(f"  Search failed: {e}")
                continue

            for item in web_results:
                url = item.url
                if url in existing_urls or url in seen_urls:
                    continue
                lower = url.lower()
                if any(skip in lower for skip in ["/about", "/contact", "/blog", "/faq", "/team", "/services", "/list-your"]):
                    continue
                if any(skip in lower for skip in ["youtube.com", "facebook.com", "instagram.com", "twitter.com"]):
                    continue
                seen_urls.add(url)
                candidates.append({"url": url, "title": item.title or "", "description": item.description or ""})

            log.info(f"  {len(web_results)} results, {len(candidates)} candidates total")

    # Shuffle for variety, then cap
    random.shuffle(candidates)

    if not candidates:
        log.info("No candidates found.")
        return 0

    # Limit to avoid excessive scraping
    candidates = candidates[:max_drafts * 6]
    log.info(f"Scraping and evaluating top {len(candidates)} candidates...")

    drafts_created = 0
    next_id = _get_next_id(posts_data)

    for cand in candidates:
        if drafts_created >= max_drafts:
            break

        log.info(f"Scraping: {cand['url'][:80]}...")

        if dry_run:
            log.info(f"  [DRY RUN] Would scrape and evaluate")
            drafts_created += 1
            continue

        listing = _scrape_listing(app, cand["url"])
        if not listing:
            continue

        if len(listing["images"]) < 1:
            log.info(f"  SKIP: no images found")
            continue

        result = _evaluate_listing(listing, niche_id, queue_summaries)
        if not result:
            continue

        draft = {
            "id": next_id,
            "type": "real-estate",
            "status": "draft",
            "text": result["text"],
            "category": result["category"],
            "price": result["price"],
            "location": result["location"],
            "source_url": listing["url"],
            "source_title": listing["title"],
            "image_urls": listing["images"][:4],
            "og_image": listing.get("og_image", ""),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "draft_reason": result["reason"],
        }

        posts_data.get("posts", []).append(draft)
        log.info(f"  Created draft #{next_id}: {result['text'][:60]}...")

        # Update queue summaries for next evaluation
        queue_summaries += f"\n- {result['text'][:80]}"
        next_id += 1
        drafts_created += 1

    if drafts_created > 0 and not dry_run:
        save_posts(posts_data)
        log.info(f"Saved {drafts_created} new real estate drafts")

    return drafts_created


def main():
    parser = argparse.ArgumentParser(description="Generate real estate listing drafts")
    parser.add_argument("--niche", default="tatamispaces")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--max-drafts", type=int, default=5)
    args = parser.parse_args()

    count = find_listings(args.niche, args.max_drafts, args.dry_run)
    log.info(f"Done. New drafts created: {count}")


if __name__ == "__main__":
    main()
