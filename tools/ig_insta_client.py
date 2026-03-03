"""
Instagram client using instagrapi (mobile API).

Replaces IGWebClient which used the web API endpoints that are now
blocked by TLS fingerprinting. instagrapi emulates an Android device
and uses Instagram's mobile private API.

Session files live at data/sessions/ig_session_{niche_id}.json
"""

import json
import logging
import os
import random
from pathlib import Path

from instagrapi import Client
from instagrapi.exceptions import (
    ChallengeRequired,
    LoginRequired,
    PleaseWaitFewMinutes,
    ClientError,
)

log = logging.getLogger(__name__)

BASE_DIR = Path(__file__).parent.parent

# Expected account IDs per niche — safety check to never engage from wrong account
EXPECTED_USER_IDS = {
    "tatamispaces": 80137319362,
    "museumstories": 47315951287,
}


class IGInstaClient:
    """Instagram client using instagrapi mobile API."""

    def __init__(self, niche_id: str = "tatamispaces"):
        self.niche_id = niche_id
        self.cl = Client()
        self.cl.delay_range = [3, 7]

        self._setup_proxy()
        self._load_or_login()
        self._validate_account()

    def _session_path(self) -> Path:
        return BASE_DIR / "data" / "sessions" / f"ig_session_{self.niche_id}.json"

    def _setup_proxy(self):
        """Configure residential proxy if available."""
        proxy_url = os.environ.get("RESIDENTIAL_PROXY")
        if proxy_url:
            self.cl.set_proxy(proxy_url)
            log.info("Using residential proxy for IG requests")
        else:
            log.warning("No RESIDENTIAL_PROXY set — using direct connection")

    def _load_or_login(self):
        """Load saved session or login with credentials."""
        session_path = self._session_path()

        if session_path.exists():
            log.info(f"Loading saved session from {session_path}")
            try:
                self.cl.load_settings(session_path)
                self.cl.login_by_sessionid(self.cl.settings.get("authorization_data", {}).get("sessionid", ""))
            except Exception:
                # Session load succeeded but may need re-auth
                pass

            # Test if session is still valid
            try:
                self.cl.get_timeline_feed()
                log.info("Saved session is valid")
                self._save_session()
                return
            except LoginRequired:
                log.warning("Saved session expired, re-logging in")
            except Exception as e:
                log.warning(f"Session test failed ({e}), re-logging in")

        # Login with credentials
        niche_key = self.niche_id.upper()
        username = os.environ.get(f"IG_USERNAME_{niche_key}")
        password = os.environ.get(f"IG_PASSWORD_{niche_key}")

        if not username or not password:
            raise ValueError(
                f"No IG credentials found. Set IG_USERNAME_{niche_key} and "
                f"IG_PASSWORD_{niche_key} in .env, or provide a session file at {session_path}"
            )

        log.info(f"Logging in as {username}")
        try:
            self.cl.login(username, password)
            self._save_session()
            log.info("Login successful, session saved")
        except ChallengeRequired:
            log.error(
                "Instagram challenge required (2FA or checkpoint). "
                "Resolve manually in the IG app, then retry."
            )
            raise
        except PleaseWaitFewMinutes:
            log.error("Instagram rate limited — wait a few minutes and retry")
            raise

    def _save_session(self):
        """Persist session to disk."""
        session_path = self._session_path()
        session_path.parent.mkdir(parents=True, exist_ok=True)
        self.cl.dump_settings(session_path)

    def _validate_account(self):
        """Verify we're logged into the expected account."""
        expected_id = EXPECTED_USER_IDS.get(self.niche_id)
        if expected_id is None:
            log.warning(f"No expected user ID configured for niche '{self.niche_id}' — skipping validation")
            return

        actual_id = int(self.cl.user_id)
        if actual_id != expected_id:
            raise RuntimeError(
                f"SAFETY: Logged into wrong account! "
                f"Expected user_id {expected_id} for {self.niche_id}, got {actual_id}. "
                f"Aborting to prevent cross-account engagement."
            )
        log.info(f"Account validated: user_id {actual_id} matches {self.niche_id}")

    def check_session(self) -> bool:
        """Verify session is valid by fetching own user info."""
        try:
            user_info = self.cl.account_info()
            log.info(f"Session valid — logged in as @{user_info.username}")
            return True
        except LoginRequired:
            log.error("Session expired — need to re-login")
            return False
        except Exception as e:
            log.error(f"Session check failed: {e}")
            return False

    def get_hashtag_feed(self, hashtag: str, max_posts: int = 12) -> list[dict]:
        """Fetch recent posts from a hashtag.

        Returns list of dicts with: shortcode, author, caption, likes, media_id, user_id, url
        """
        # Vary the amount slightly to avoid uniform request patterns
        amount = random.randint(max(5, max_posts - 3), max_posts + 2)

        try:
            medias = self.cl.hashtag_medias_recent(hashtag, amount=amount)
        except PleaseWaitFewMinutes:
            log.warning(f"#{hashtag}: rate limited, skipping")
            return []
        except ChallengeRequired:
            log.error(f"#{hashtag}: challenge required — resolve manually")
            return []
        except Exception as e:
            log.error(f"#{hashtag}: fetch failed: {e}")
            return []

        posts = []
        for media in medias[:max_posts]:
            caption_text = media.caption_text or ""

            post = {
                "shortcode": media.code,
                "media_id": str(media.pk),
                "author": media.user.username if media.user else "",
                "user_id": str(media.user.pk) if media.user else "",
                "caption": caption_text[:500],
                "likes": media.like_count or 0,
                "url": f"https://www.instagram.com/p/{media.code}/",
            }
            if post["shortcode"] and post["author"]:
                posts.append(post)

        log.info(f"#{hashtag}: {len(posts)} posts")
        self._save_session()
        return posts

    def like_post(self, media_id: str) -> bool:
        """Like a post by media ID."""
        try:
            self.cl.media_like(int(media_id))
            self._save_session()
            return True
        except PleaseWaitFewMinutes:
            log.warning(f"Like {media_id}: rate limited")
            return False
        except Exception as e:
            log.error(f"Like {media_id} failed: {e}")
            return False

    def comment_post(self, media_id: str, comment_text: str) -> bool:
        """Comment on a post."""
        try:
            self.cl.media_comment(int(media_id), comment_text)
            self._save_session()
            return True
        except PleaseWaitFewMinutes:
            log.warning(f"Comment {media_id}: rate limited")
            return False
        except Exception as e:
            log.error(f"Comment {media_id} failed: {e}")
            return False

    def follow_user(self, user_id: str) -> bool:
        """Follow a user by their user ID."""
        try:
            self.cl.user_follow(int(user_id))
            self._save_session()
            return True
        except PleaseWaitFewMinutes:
            log.warning(f"Follow {user_id}: rate limited")
            return False
        except Exception as e:
            log.error(f"Follow {user_id} failed: {e}")
            return False
