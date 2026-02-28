"""
Instagram Web API client using session cookies.

Replaces Playwright browser automation for IG engagement.
Loads cookies extracted from a real Chrome session and makes direct
HTTP requests to Instagram's private web API endpoints.

Cookie files live at data/cookies/ig_cookies_{niche}.json

Proxy support: set RESIDENTIAL_PROXY in .env to route through a residential IP.
Format: http://user:pass@host:port or socks5://user:pass@host:port
"""

import json
import logging
import os
import random
import time
from pathlib import Path

import requests

log = logging.getLogger(__name__)

BASE_DIR = Path(__file__).parent.parent

# Instagram web app ID (constant, same for all users)
IG_APP_ID = "936619743392459"

# Chrome user agents to rotate
_CHROME_UAS = [
    (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/133.0.0.0 Safari/537.36"
    ),
    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/133.0.0.0 Safari/537.36"
    ),
    (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/132.0.0.0 Safari/537.36"
    ),
]

# Max retries on 429
MAX_RETRIES = 3
RETRY_BASE_DELAY = 10  # seconds


class IGWebClient:
    """HTTP client for Instagram's private web API."""

    def __init__(self, niche_id: str = "tatamispaces"):
        self.niche_id = niche_id
        self.session = requests.Session()
        self._setup_proxy()
        self._load_cookies()

    def _setup_proxy(self):
        """Configure residential proxy if available."""
        proxy_url = os.environ.get("RESIDENTIAL_PROXY", "").strip()
        if proxy_url:
            self.session.proxies = {
                "http": proxy_url,
                "https": proxy_url,
            }
            # Mask credentials in log
            masked = proxy_url.split("@")[-1] if "@" in proxy_url else proxy_url
            log.info(f"Using residential proxy: {masked}")
        else:
            log.warning(
                "No RESIDENTIAL_PROXY set — IG may block datacenter IPs. "
                "Set RESIDENTIAL_PROXY=http://user:pass@host:port in .env"
            )

    def _cookie_path(self) -> Path:
        return BASE_DIR / "data" / "cookies" / f"ig_cookies_{self.niche_id}.json"

    def _load_cookies(self):
        """Load cookies from JSON file into requests session."""
        path = self._cookie_path()
        if not path.exists():
            raise FileNotFoundError(
                f"IG cookies not found: {path}\n"
                f"Extract cookies from Chrome and save to {path}"
            )

        with open(path) as f:
            cookie_dict = json.load(f)

        if "sessionid" not in cookie_dict:
            raise ValueError(f"No sessionid in {path} — cookie extraction incomplete")

        self.ds_user_id = cookie_dict.get("ds_user_id", "")
        csrf_token = cookie_dict.get("csrftoken", "")

        # Set cookies on session
        for name, value in cookie_dict.items():
            self.session.cookies.set(name, value, domain=".instagram.com")

        # Set required headers with a random UA
        self.session.headers.update({
            "User-Agent": random.choice(_CHROME_UAS),
            "X-CSRFToken": csrf_token,
            "X-IG-App-ID": IG_APP_ID,
            "X-Requested-With": "XMLHttpRequest",
            "Referer": "https://www.instagram.com/",
            "Origin": "https://www.instagram.com",
            "Accept": "*/*",
            "Accept-Language": "en-US,en;q=0.9",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
            "Sec-Ch-Ua-Platform": '"macOS"',
        })

        log.info(f"Loaded IG cookies for user {self.ds_user_id}")

    def _request_with_retry(self, method: str, url: str, **kwargs) -> requests.Response | None:
        """Make a request with retry on 429."""
        kwargs.setdefault("timeout", 15)
        for attempt in range(MAX_RETRIES + 1):
            try:
                resp = getattr(self.session, method)(url, **kwargs)
                if resp.status_code != 429:
                    return resp
                # 429 — back off and retry
                delay = RETRY_BASE_DELAY * (2 ** attempt) + random.uniform(1, 5)
                if attempt < MAX_RETRIES:
                    log.warning(f"429 on {url.split('?')[0]} — retry {attempt + 1}/{MAX_RETRIES} in {delay:.0f}s")
                    time.sleep(delay)
                else:
                    log.error(f"429 on {url.split('?')[0]} — all {MAX_RETRIES} retries exhausted")
                    return resp
            except Exception as e:
                log.error(f"Request failed: {e}")
                if attempt < MAX_RETRIES:
                    time.sleep(RETRY_BASE_DELAY)
                else:
                    return None
        return None

    def _check_login(self, resp: requests.Response) -> bool:
        """Check if response indicates we're logged out."""
        if resp.status_code == 401:
            log.error("IG session expired (401)")
            return False
        if resp.status_code == 403:
            log.error("IG session forbidden (403) — may need new cookies")
            return False
        # Check for login redirect in JSON
        try:
            data = resp.json()
            if data.get("require_login"):
                log.error("IG requires login — cookies expired")
                return False
        except (ValueError, KeyError):
            pass
        return True

    def get_hashtag_feed(self, hashtag: str, max_posts: int = 12) -> list[dict]:
        """Fetch recent posts from a hashtag.

        Returns list of dicts with: shortcode, author, caption, likes, media_id, user_id
        """
        url = f"https://www.instagram.com/api/v1/tags/web_info/?tag_name={hashtag}"
        resp = self._request_with_retry("get", url)
        if resp is None:
            return []
        if not self._check_login(resp):
            return []
        if resp.status_code != 200:
            log.warning(f"Hashtag {hashtag} returned {resp.status_code}")
            return []

        try:
            data = resp.json()
        except Exception as e:
            log.error(f"Hashtag {hashtag} JSON parse failed: {e}")
            return []

        posts = []
        # Extract from sections -> media entries
        sections = data.get("data", {}).get("recent", {}).get("sections", [])
        if not sections:
            # Try top posts
            sections = data.get("data", {}).get("top", {}).get("sections", [])
        if not sections:
            # Fallback: try direct media array
            sections = data.get("sections", [])

        for section in sections:
            medias = section.get("layout_content", {}).get("medias", [])
            for item in medias:
                media = item.get("media", {})
                if not media:
                    continue

                shortcode = media.get("code", "")
                user = media.get("user", {})
                caption_obj = media.get("caption") or {}
                caption_text = caption_obj.get("text", "") if isinstance(caption_obj, dict) else ""

                post = {
                    "shortcode": shortcode,
                    "media_id": str(media.get("pk", "")),
                    "author": user.get("username", ""),
                    "user_id": str(user.get("pk", "")),
                    "caption": caption_text[:500],
                    "likes": media.get("like_count", 0),
                    "url": f"https://www.instagram.com/p/{shortcode}/",
                }
                if post["shortcode"] and post["author"]:
                    posts.append(post)

                if len(posts) >= max_posts:
                    break
            if len(posts) >= max_posts:
                break

        log.info(f"#{hashtag}: {len(posts)} posts")
        return posts

    def like_post(self, media_id: str) -> bool:
        """Like a post by media ID."""
        url = f"https://www.instagram.com/api/v1/web/likes/{media_id}/like/"
        resp = self._request_with_retry("post", url)
        if resp is None:
            return False
        if not self._check_login(resp):
            return False
        if resp.status_code == 200:
            return True
        log.warning(f"Like {media_id} returned {resp.status_code}: {resp.text[:200]}")
        return False

    def comment_post(self, media_id: str, comment_text: str) -> bool:
        """Comment on a post."""
        url = f"https://www.instagram.com/api/v1/web/comments/{media_id}/add/"
        resp = self._request_with_retry("post", url, data={"comment_text": comment_text})
        if resp is None:
            return False
        if not self._check_login(resp):
            return False
        if resp.status_code == 200:
            return True
        log.warning(f"Comment {media_id} returned {resp.status_code}: {resp.text[:200]}")
        return False

    def follow_user(self, user_id: str) -> bool:
        """Follow a user by their user ID."""
        url = f"https://www.instagram.com/api/v1/friendships/create/{user_id}/"
        resp = self._request_with_retry("post", url)
        if resp is None:
            return False
        if not self._check_login(resp):
            return False
        if resp.status_code == 200:
            return True
        log.warning(f"Follow {user_id} returned {resp.status_code}: {resp.text[:200]}")
        return False

    def check_session(self) -> bool:
        """Verify session is valid by checking our own profile."""
        url = f"https://www.instagram.com/api/v1/users/{self.ds_user_id}/info/"
        try:
            resp = self.session.get(url, timeout=15)
            if resp.status_code == 200:
                data = resp.json()
                username = data.get("user", {}).get("username", "unknown")
                log.info(f"Session valid — logged in as @{username}")
                return True
            log.error(f"Session check failed: {resp.status_code}")
            return False
        except Exception as e:
            log.error(f"Session check failed: {e}")
            return False
