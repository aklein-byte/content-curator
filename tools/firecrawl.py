"""
Firecrawl integration for scraping JS-heavy design websites.
"""

import os
import httpx
from typing import Optional
from dataclasses import dataclass

FIRECRAWL_API_KEY = os.environ.get("FIRECRAWL_API_KEY", "")
FIRECRAWL_BASE_URL = "https://api.firecrawl.dev/v1"


@dataclass
class ScrapedPage:
    """Result from scraping a page."""
    url: str
    title: Optional[str]
    markdown: str
    images: list[dict]  # List of {url, alt, context}
    links: list[str]
    success: bool
    error: Optional[str] = None


async def scrape_url(url: str, include_images: bool = True) -> ScrapedPage:
    """
    Scrape a URL using Firecrawl.
    Returns markdown content and extracted images.
    """
    if not FIRECRAWL_API_KEY:
        return ScrapedPage(
            url=url,
            title=None,
            markdown="",
            images=[],
            links=[],
            success=False,
            error="FIRECRAWL_API_KEY not set",
        )

    async with httpx.AsyncClient(timeout=60.0) as client:
        try:
            response = await client.post(
                f"{FIRECRAWL_BASE_URL}/scrape",
                headers={
                    "Authorization": f"Bearer {FIRECRAWL_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "url": url,
                    "formats": ["markdown", "links"],
                    "onlyMainContent": True,
                    "includeTags": ["img", "picture", "figure"],
                    "waitFor": 3000,  # Wait for JS to load
                },
            )

            if response.status_code != 200:
                return ScrapedPage(
                    url=url,
                    title=None,
                    markdown="",
                    images=[],
                    links=[],
                    success=False,
                    error=f"API error: {response.status_code} - {response.text[:200]}",
                )

            data = response.json()

            if not data.get("success"):
                return ScrapedPage(
                    url=url,
                    title=None,
                    markdown="",
                    images=[],
                    links=[],
                    success=False,
                    error=data.get("error", "Unknown error"),
                )

            result = data.get("data", {})

            # Extract images from markdown
            images = extract_images_from_markdown(result.get("markdown", ""), url)

            return ScrapedPage(
                url=url,
                title=result.get("metadata", {}).get("title"),
                markdown=result.get("markdown", ""),
                images=images,
                links=result.get("links", []),
                success=True,
            )

        except httpx.TimeoutException:
            return ScrapedPage(
                url=url,
                title=None,
                markdown="",
                images=[],
                links=[],
                success=False,
                error="Request timed out",
            )
        except Exception as e:
            return ScrapedPage(
                url=url,
                title=None,
                markdown="",
                images=[],
                links=[],
                success=False,
                error=str(e),
            )


def extract_images_from_markdown(markdown: str, base_url: str) -> list[dict]:
    """
    Extract image URLs and their context from markdown.
    Returns list of {url, alt, context} dicts.
    """
    import re
    from urllib.parse import urljoin

    images = []

    # Match markdown images: ![alt](url)
    pattern = r'!\[([^\]]*)\]\(([^)]+)\)'

    for match in re.finditer(pattern, markdown):
        alt = match.group(1)
        url = match.group(2)

        # Make URL absolute if relative
        if url.startswith("/"):
            url = urljoin(base_url, url)

        # Skip tiny images, icons, logos
        if any(skip in url.lower() for skip in ["logo", "icon", "avatar", "thumb", "1x1", "pixel"]):
            continue

        # Get surrounding context (text before and after)
        start = max(0, match.start() - 200)
        end = min(len(markdown), match.end() + 200)
        context = markdown[start:end]

        images.append({
            "url": url,
            "alt": alt,
            "context": context.strip(),
        })

    return images


async def search_images(
    query: str,
    num_results: int = 10,
    site: Optional[str] = None,
) -> list[dict]:
    """
    Search for images using Firecrawl's search endpoint.
    """
    if not FIRECRAWL_API_KEY:
        return []

    search_query = query
    if site:
        search_query = f"site:{site} {query}"

    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            response = await client.post(
                f"{FIRECRAWL_BASE_URL}/search",
                headers={
                    "Authorization": f"Bearer {FIRECRAWL_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "query": search_query,
                    "limit": num_results,
                },
            )

            if response.status_code != 200:
                return []

            data = response.json()
            return data.get("data", [])

        except Exception:
            return []


async def batch_scrape(urls: list[str]) -> list[ScrapedPage]:
    """
    Scrape multiple URLs concurrently.
    Uses Firecrawl batch endpoint for efficiency.
    """
    if not urls:
        return []

    if not FIRECRAWL_API_KEY:
        return [
            ScrapedPage(url=url, title=None, markdown="", images=[], links=[],
                       success=False, error="FIRECRAWL_API_KEY not set")
            for url in urls
        ]

    async with httpx.AsyncClient(timeout=120.0) as client:
        try:
            response = await client.post(
                f"{FIRECRAWL_BASE_URL}/batch/scrape",
                headers={
                    "Authorization": f"Bearer {FIRECRAWL_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "urls": urls,
                    "formats": ["markdown", "links"],
                    "onlyMainContent": True,
                },
            )

            if response.status_code != 200:
                return [
                    ScrapedPage(url=url, title=None, markdown="", images=[], links=[],
                               success=False, error=f"API error: {response.status_code}")
                    for url in urls
                ]

            data = response.json()

            # Poll for results if async
            if data.get("id"):
                return await poll_batch_results(data["id"], urls)

            # Direct results
            results = []
            for item in data.get("data", []):
                url = item.get("url", urls[len(results)] if len(results) < len(urls) else "")
                images = extract_images_from_markdown(item.get("markdown", ""), url)
                results.append(ScrapedPage(
                    url=url,
                    title=item.get("metadata", {}).get("title"),
                    markdown=item.get("markdown", ""),
                    images=images,
                    links=item.get("links", []),
                    success=True,
                ))
            return results

        except Exception as e:
            return [
                ScrapedPage(url=url, title=None, markdown="", images=[], links=[],
                           success=False, error=str(e))
                for url in urls
            ]


async def poll_batch_results(batch_id: str, urls: list[str], max_attempts: int = 30) -> list[ScrapedPage]:
    """Poll for batch scrape results."""
    import asyncio

    async with httpx.AsyncClient(timeout=30.0) as client:
        for _ in range(max_attempts):
            response = await client.get(
                f"{FIRECRAWL_BASE_URL}/batch/scrape/{batch_id}",
                headers={"Authorization": f"Bearer {FIRECRAWL_API_KEY}"},
            )

            if response.status_code != 200:
                break

            data = response.json()
            status = data.get("status")

            if status == "completed":
                results = []
                for item in data.get("data", []):
                    url = item.get("url", urls[len(results)] if len(results) < len(urls) else "")
                    images = extract_images_from_markdown(item.get("markdown", ""), url)
                    results.append(ScrapedPage(
                        url=url,
                        title=item.get("metadata", {}).get("title"),
                        markdown=item.get("markdown", ""),
                        images=images,
                        links=item.get("links", []),
                        success=True,
                    ))
                return results

            if status == "failed":
                break

            await asyncio.sleep(2)

    return [
        ScrapedPage(url=url, title=None, markdown="", images=[], links=[],
                   success=False, error="Batch scrape timed out or failed")
        for url in urls
    ]
