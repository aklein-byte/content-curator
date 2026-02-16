"""
X/Twitter Official API v2 client.

Uses OAuth 1.0a (user context) for posting, search, like, reply, follow.
Uses OAuth 2.0 PKCE for bookmarks.

Credentials are resolved per-niche via x_api_env in config/niches.py.
Call set_niche("museumstories") before API calls to switch accounts.
Default (tatamispaces): X_API_CONSUMER_KEY, X_API_CONSUMER_SECRET, etc.
"""

import os
import hashlib
import logging
import time
import requests
from pathlib import Path
from requests_oauthlib import OAuth1
from dataclasses import dataclass
from typing import Optional

log = logging.getLogger(__name__)

API_BASE = "https://api.twitter.com/2"

# Per-niche credential support. Call set_niche("museumstories") before using
# any API functions to switch X API accounts.
# Env var names are configured per-niche in config/niches.py under x_api_env.
_active_niche: str | None = None
_niche_env_map: dict | None = None  # cached from niches.py


def set_niche(niche_id: str | None):
    """Set the active niche for credential resolution. Clears user ID cache."""
    global _active_niche, _niche_env_map
    _active_niche = niche_id
    _niche_env_map = None  # reset so it re-reads from config
    # Clear cached user ID since it's per-account
    if hasattr(_get_user_id, "_cached"):
        del _get_user_id._cached


def _to_orig_url(url: str) -> str:
    """Upgrade a Twitter image URL to original (full) resolution.

    pbs.twimg.com/media/XXX.jpg -> pbs.twimg.com/media/XXX?format=jpg&name=orig
    """
    if "pbs.twimg.com" not in url:
        return url
    if "name=orig" in url:
        return url
    # Strip existing query params and extension, rebuild with orig
    base = url.split("?")[0]
    if base.endswith((".jpg", ".jpeg", ".png", ".webp")):
        ext = base.rsplit(".", 1)[-1]
        base = base.rsplit(".", 1)[0]
        return f"{base}?format={ext}&name=orig"
    return url


def _get_env_map() -> dict:
    """Get env var name mapping for the active niche."""
    global _niche_env_map
    if _niche_env_map is not None:
        return _niche_env_map

    # Default env var names (tatamispaces)
    defaults = {
        "consumer_key": "X_API_CONSUMER_KEY",
        "consumer_secret": "X_API_CONSUMER_SECRET",
        "access_token": "X_API_ACCESS_TOKEN",
        "access_token_secret": "X_API_ACCESS_TOKEN_SECRET",
    }

    if _active_niche:
        try:
            from config.niches import get_niche
            niche = get_niche(_active_niche)
            _niche_env_map = niche.get("x_api_env", defaults)
        except Exception:
            _niche_env_map = defaults
    else:
        _niche_env_map = defaults

    return _niche_env_map


def _get_auth() -> OAuth1:
    env_map = _get_env_map()
    return OAuth1(
        os.environ[env_map["consumer_key"]],
        os.environ[env_map["consumer_secret"]],
        os.environ[env_map["access_token"]],
        os.environ[env_map["access_token_secret"]],
    )


def _get_user_id() -> str:
    """Get the authenticated user's ID (cached after first call)."""
    if not hasattr(_get_user_id, "_cached"):
        r = requests.get(f"{API_BASE}/users/me", auth=_get_auth(), timeout=10)
        r.raise_for_status()
        _get_user_id._cached = r.json()["data"]["id"]
    return _get_user_id._cached


@dataclass
class XPost:
    """A post from X with metadata."""
    post_id: str
    author_handle: str
    author_name: str
    author_id: str
    text: str
    image_urls: list[str]
    likes: int
    reposts: int
    replies: int
    views: int
    language: Optional[str]
    created_at: Optional[str]
    author_followers: int = 0


def search_posts(query: str, max_results: int = 15) -> list[XPost]:
    """Search recent tweets via official API v2.

    Note: API v2 uses different operators than web search:
      - has:images (not filter:images)
      - has:media, has:videos
      - -is:retweet
      - lang:ja
      - No min_faves — filter by metrics after fetching

    Rate limit: 300 requests / 15 min (Basic tier).
    """
    auth = _get_auth()

    r = requests.get(
        f"{API_BASE}/tweets/search/recent",
        params={
            "query": query,
            "max_results": min(max_results, 100),
            "tweet.fields": "public_metrics,author_id,created_at,lang,attachments",
            "expansions": "author_id,attachments.media_keys",
            "media.fields": "url,type,preview_image_url",
            "user.fields": "username,name,public_metrics",
        },
        auth=auth,
        timeout=15,
    )

    if r.status_code == 429:
        reset = int(r.headers.get("x-rate-limit-reset", 0))
        wait = max(reset - int(time.time()), 60)
        log.warning(f"Rate limited on search. Resets in {wait}s")
        return []

    if r.status_code != 200:
        log.error(f"Search failed: {r.status_code} {r.text[:200]}")
        return []

    data = r.json()
    if "data" not in data:
        return []

    # Build lookup maps
    users = {u["id"]: u for u in data.get("includes", {}).get("users", [])}
    media_map = {m["media_key"]: m for m in data.get("includes", {}).get("media", [])}

    posts = []
    for tweet in data["data"]:
        user = users.get(tweet.get("author_id"), {})
        metrics = tweet.get("public_metrics", {})

        # Extract image URLs from media attachments (upgrade to original resolution)
        image_urls = []
        media_keys = tweet.get("attachments", {}).get("media_keys", [])
        for mk in media_keys:
            m = media_map.get(mk, {})
            if m.get("type") == "photo":
                url = m.get("url") or m.get("preview_image_url")
                if url:
                    image_urls.append(_to_orig_url(url))

        user_metrics = user.get("public_metrics", {})
        posts.append(XPost(
            post_id=tweet["id"],
            author_handle=user.get("username", ""),
            author_name=user.get("name", ""),
            author_id=tweet.get("author_id", ""),
            text=tweet.get("text", ""),
            image_urls=image_urls,
            likes=metrics.get("like_count", 0),
            reposts=metrics.get("retweet_count", 0),
            replies=metrics.get("reply_count", 0),
            views=metrics.get("impression_count", 0),
            language=tweet.get("lang"),
            created_at=tweet.get("created_at"),
            author_followers=user_metrics.get("followers_count", 0),
        ))

    remaining = r.headers.get("x-rate-limit-remaining", "?")
    log.debug(f"Search returned {len(posts)} tweets (rate limit remaining: {remaining})")
    return posts


def like_post(tweet_id: str) -> bool:
    """Like a tweet. Returns True on success."""
    user_id = _get_user_id()
    r = requests.post(
        f"{API_BASE}/users/{user_id}/likes",
        json={"tweet_id": tweet_id},
        auth=_get_auth(),
        timeout=10,
    )
    if r.status_code == 200:
        liked = r.json().get("data", {}).get("liked", False)
        if liked:
            log.debug(f"Liked tweet {tweet_id}")
        return liked
    elif r.status_code == 429:
        log.warning("Rate limited on like")
        return False
    else:
        log.error(f"Like failed: {r.status_code} {r.text[:200]}")
        return False


def follow_user(target_user_id: str) -> bool:
    """Follow a user by ID. Returns True on success."""
    user_id = _get_user_id()
    r = requests.post(
        f"{API_BASE}/users/{user_id}/following",
        json={"target_user_id": target_user_id},
        auth=_get_auth(),
        timeout=10,
    )
    if r.status_code == 200:
        following = r.json().get("data", {}).get("following", False)
        if following:
            log.debug(f"Followed user {target_user_id}")
        return following
    elif r.status_code == 429:
        log.warning("Rate limited on follow")
        return False
    else:
        log.error(f"Follow failed: {r.status_code} {r.text[:200]}")
        return False


def reply_to_post(tweet_id: str, text: str) -> str | None:
    """Reply to a tweet. Returns the reply tweet ID or None."""
    r = requests.post(
        f"{API_BASE}/tweets",
        json={
            "text": text,
            "reply": {"in_reply_to_tweet_id": tweet_id},
        },
        auth=_get_auth(),
        timeout=15,
    )
    if r.status_code in (200, 201):
        reply_id = r.json().get("data", {}).get("id")
        log.debug(f"Replied to {tweet_id}: {reply_id}")
        return reply_id
    elif r.status_code == 429:
        log.warning("Rate limited on reply")
        return None
    else:
        log.error(f"Reply failed: {r.status_code} {r.text[:200]}")
        return None


def upload_media(file_path: str) -> str | None:
    """Upload an image via v1.1 media upload endpoint. Returns media_id string."""
    auth = _get_auth()
    with open(file_path, "rb") as f:
        r = requests.post(
            "https://upload.twitter.com/1.1/media/upload.json",
            files={"media": f},
            auth=auth,
            timeout=60,
        )
    if r.status_code in (200, 201, 202):
        media_id = r.json().get("media_id_string")
        log.debug(f"Uploaded media {file_path}: {media_id}")
        return media_id
    else:
        log.error(f"Media upload failed: {r.status_code} {r.text[:200]}")
        return None


def create_tweet(text: str, media_ids: list[str] | None = None, reply_to: str | None = None, community_id: str | None = None, quote_tweet_id: str | None = None) -> str | None:
    """Create a tweet via v2 API. Returns tweet ID or None."""
    payload = {"text": text}
    if media_ids:
        payload["media"] = {"media_ids": media_ids}
    if reply_to:
        payload["reply"] = {"in_reply_to_tweet_id": reply_to}
    if community_id:
        payload["community_id"] = community_id
    if quote_tweet_id:
        payload["quote_tweet_id"] = quote_tweet_id

    r = requests.post(
        f"{API_BASE}/tweets",
        json=payload,
        auth=_get_auth(),
        timeout=15,
    )
    if r.status_code in (200, 201):
        tweet_id = r.json().get("data", {}).get("id")
        log.info(f"Posted tweet: {tweet_id}")
        return tweet_id
    elif r.status_code == 429:
        log.warning("Rate limited on tweet creation")
        return None
    else:
        log.error(f"Tweet creation failed: {r.status_code} {r.text[:300]}")
        return None


@dataclass
class XMention:
    """A mention/reply to one of our tweets."""
    tweet_id: str
    text: str
    author_handle: str
    author_id: str
    created_at: str
    parent_tweet_id: Optional[str]
    parent_text: Optional[str]
    conversation_id: Optional[str]


def get_mentions(max_results: int = 50, since_id: Optional[str] = None) -> list[XMention]:
    """Get recent mentions of the authenticated user via v2 API.

    Returns mentions with parent tweet text resolved via expansions.
    """
    user_id = _get_user_id()
    params = {
        "max_results": min(max_results, 100),
        "tweet.fields": "created_at,in_reply_to_user_id,referenced_tweets,conversation_id,author_id",
        "expansions": "author_id,referenced_tweets.id",
        "user.fields": "username",
    }
    if since_id:
        params["since_id"] = since_id

    r = requests.get(
        f"{API_BASE}/users/{user_id}/mentions",
        params=params,
        auth=_get_auth(),
        timeout=15,
    )

    if r.status_code == 429:
        log.warning("Rate limited on mentions")
        return []
    if r.status_code != 200:
        log.error(f"Mentions failed: {r.status_code} {r.text[:200]}")
        return []

    data = r.json()
    if "data" not in data:
        return []

    users = {u["id"]: u["username"] for u in data.get("includes", {}).get("users", [])}
    ref_tweets = {t["id"]: t.get("text", "") for t in data.get("includes", {}).get("tweets", [])}

    mentions = []
    for tweet in data["data"]:
        author = users.get(tweet["author_id"], "")
        # Skip our own tweets (replies we already made)
        if tweet["author_id"] == user_id:
            continue

        # Find parent tweet from referenced_tweets
        parent_id = None
        parent_text = None
        for ref in tweet.get("referenced_tweets", []):
            if ref["type"] == "replied_to":
                parent_id = ref["id"]
                parent_text = ref_tweets.get(parent_id)
                break

        mentions.append(XMention(
            tweet_id=tweet["id"],
            text=tweet.get("text", ""),
            author_handle=author,
            author_id=tweet["author_id"],
            created_at=tweet.get("created_at", ""),
            parent_tweet_id=parent_id,
            parent_text=parent_text,
            conversation_id=tweet.get("conversation_id"),
        ))

    remaining = r.headers.get("x-rate-limit-remaining", "?")
    log.debug(f"Mentions: {len(mentions)} (rate limit remaining: {remaining})")
    return mentions


def get_own_recent_tweets(max_results: int = 20) -> list[dict]:
    """Fetch our own recent original tweets (no replies/retweets).

    Returns list of dicts with 'id', 'text', 'created_at'.
    Used for deduplication before posting.
    """
    user_id = _get_user_id()
    r = requests.get(
        f"{API_BASE}/users/{user_id}/tweets",
        params={
            "max_results": min(max_results, 100),
            "tweet.fields": "created_at,text",
            "exclude": "replies,retweets",
        },
        auth=_get_auth(),
        timeout=15,
    )
    if r.status_code == 429:
        log.warning("Rate limited on timeline fetch")
        return []
    if r.status_code != 200:
        log.error(f"Timeline fetch failed: {r.status_code} {r.text[:200]}")
        return []
    return r.json().get("data", [])


def get_following(max_results: int = 200) -> list[dict]:
    """Get list of accounts we follow.

    Returns list of dicts with 'id', 'username', 'name', 'description'.
    Handles pagination for accounts > 1000.
    Rate limit: 15 requests / 15 min.
    """
    user_id = _get_user_id()
    following = []
    pagination_token = None

    while True:
        params = {
            "max_results": min(max_results - len(following), 1000),
            "user.fields": "username,name,description,public_metrics,created_at",
        }
        if pagination_token:
            params["pagination_token"] = pagination_token

        r = requests.get(
            f"{API_BASE}/users/{user_id}/following",
            params=params,
            auth=_get_auth(),
            timeout=15,
        )

        if r.status_code == 429:
            log.warning("Rate limited on get_following")
            break
        if r.status_code != 200:
            log.error(f"Get following failed: {r.status_code} {r.text[:200]}")
            break

        data = r.json()
        for u in data.get("data", []):
            following.append({
                "id": u["id"],
                "username": u.get("username", ""),
                "name": u.get("name", ""),
                "description": u.get("description", ""),
                "followers_count": u.get("public_metrics", {}).get("followers_count", 0),
                "following_count": u.get("public_metrics", {}).get("following_count", 0),
                "tweet_count": u.get("public_metrics", {}).get("tweet_count", 0),
                "created_at": u.get("created_at", ""),
            })

        pagination_token = data.get("meta", {}).get("next_token")
        if not pagination_token or len(following) >= max_results:
            break

    log.info(f"Following {len(following)} accounts")
    return following


def unfollow_user(target_user_id: str) -> bool:
    """Unfollow a user by ID. Returns True on success."""
    user_id = _get_user_id()
    r = requests.delete(
        f"{API_BASE}/users/{user_id}/following/{target_user_id}",
        auth=_get_auth(),
        timeout=10,
    )
    if r.status_code == 200:
        unfollowed = not r.json().get("data", {}).get("following", True)
        if unfollowed:
            log.debug(f"Unfollowed user {target_user_id}")
        return unfollowed
    elif r.status_code == 429:
        log.warning("Rate limited on unfollow")
        return False
    else:
        log.error(f"Unfollow failed: {r.status_code} {r.text[:200]}")
        return False


def get_user_recent_tweets(user_id: str, max_results: int = 5) -> list[dict]:
    """Fetch recent tweets from a specific user. Returns list of dicts with 'id', 'text', 'created_at'."""
    r = requests.get(
        f"{API_BASE}/users/{user_id}/tweets",
        params={
            "max_results": min(max(max_results, 5), 100),
            "tweet.fields": "created_at,text,public_metrics",
            "exclude": "replies,retweets",
        },
        auth=_get_auth(),
        timeout=15,
    )
    if r.status_code == 429:
        log.warning(f"Rate limited fetching tweets for user {user_id}")
        return []
    if r.status_code != 200:
        log.warning(f"Failed to fetch tweets for user {user_id}: {r.status_code}")
        return []
    return r.json().get("data", [])


def get_liking_users(tweet_id: str, max_results: int = 20) -> list[dict]:
    """Get users who liked a tweet. Returns list of {id, username, name}."""
    r = requests.get(
        f"{API_BASE}/tweets/{tweet_id}/liking_users",
        params={
            "max_results": min(max_results, 100),
            "user.fields": "username,name,public_metrics",
        },
        auth=_get_auth(),
        timeout=15,
    )
    if r.status_code == 429:
        log.warning("Rate limited on liking_users")
        return []
    if r.status_code != 200:
        log.warning(f"Liking users failed for {tweet_id}: {r.status_code}")
        return []
    return r.json().get("data", [])


def download_image(url: str, save_dir: str = "data/images") -> str | None:
    """Download image from URL to local path. Returns local file path or None."""
    Path(save_dir).mkdir(parents=True, exist_ok=True)

    ext = ".jpg"
    if ".png" in url:
        ext = ".png"
    elif ".webp" in url:
        ext = ".webp"
    filename = hashlib.md5(url.encode()).hexdigest() + ext
    save_path = str(Path(save_dir) / filename)

    if Path(save_path).exists():
        return save_path

    # Upgrade Twitter image URLs to original resolution
    url = _to_orig_url(url)

    try:
        resp = requests.get(
            url,
            headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"},
            timeout=30,
            allow_redirects=True,
        )
        if resp.status_code == 200 and len(resp.content) > 10_000:
            with open(save_path, "wb") as f:
                f.write(resp.content)
            return save_path
    except Exception as e:
        log.error(f"Failed to download image {url}: {e}")
    return None


def post_thread(
    tweets: list[dict],
    delay_seconds: tuple[int, int] = (120, 300),
    community_id: str | None = None,
) -> list[str]:
    """Post a thread (chain of tweets) via X API v2.

    Each entry in tweets should have:
      - "text": tweet text (required)
      - "image_paths": list of local image file paths (optional)

    community_id is applied to the first tweet only (X Communities).

    Returns list of posted tweet IDs. May be shorter than tweets if a post fails.
    """
    import time as _time
    import random

    if not tweets:
        return []

    posted_ids = []

    for i, tweet_data in enumerate(tweets):
        text = tweet_data["text"]
        image_paths = tweet_data.get("image_paths", [])

        # Upload images
        media_ids = []
        for img_path in image_paths:
            mid = upload_media(img_path)
            if mid:
                media_ids.append(mid)
            else:
                log.warning(f"Thread tweet {i+1}: failed to upload {img_path}")

        # Reply to previous tweet (except first)
        reply_to = posted_ids[-1] if posted_ids else None

        tweet_id = create_tweet(
            text=text,
            media_ids=media_ids if media_ids else None,
            reply_to=reply_to,
            community_id=community_id if i == 0 else None,
        )

        if tweet_id:
            posted_ids.append(tweet_id)
            log.info(f"Thread {i+1}/{len(tweets)}: {tweet_id}")
        else:
            log.error(f"Thread broken at tweet {i+1}/{len(tweets)}")
            break

        # Delay between tweets (skip after last)
        if i < len(tweets) - 1:
            delay = random.uniform(*delay_seconds)
            log.info(f"Waiting {delay:.0f}s before next tweet...")
            _time.sleep(delay)

    return posted_ids


def get_user_id_by_handle(handle: str) -> str | None:
    """Look up a user ID by handle (without @)."""
    handle = handle.lstrip("@")
    r = requests.get(
        f"{API_BASE}/users/by/username/{handle}",
        auth=_get_auth(),
        timeout=10,
    )
    if r.status_code == 200:
        return r.json().get("data", {}).get("id")
    log.warning(f"User lookup failed for @{handle}: {r.status_code}")
    return None


def pin_tweet(tweet_id: str) -> bool:
    """Pin a tweet to the profile via v1.1 API. Returns True on success."""
    r = requests.post(
        "https://api.twitter.com/1.1/account/pin_tweet.json",
        data={"id": tweet_id},
        auth=_get_auth(),
        timeout=10,
    )
    if r.status_code == 200:
        log.info(f"Pinned tweet {tweet_id}")
        return True
    log.error(f"Pin tweet failed: {r.status_code} {r.text[:200]}")
    return False


# ---------------------------------------------------------------------------
# OAuth 2.0 PKCE — required for bookmarks endpoint
# ---------------------------------------------------------------------------

_OAUTH2_TOKEN_FILE = Path(__file__).parent.parent / "data" / ".oauth2_tokens.json"
_OAUTH2_TOKEN_URL = "https://api.x.com/2/oauth2/token"
OAUTH2_CALLBACK_URL = "http://127.0.0.1:9876/callback"
OAUTH2_SCOPES = "bookmark.read tweet.read users.read offline.access"


def _get_oauth2_creds() -> tuple[str, str]:
    """Get OAuth 2.0 Client ID and Client Secret from env."""
    env_map = _get_env_map()
    client_id = os.environ.get(env_map.get("oauth2_client_id", "X_OAUTH2_CLIENT_ID"), "")
    client_secret = os.environ.get(env_map.get("oauth2_client_secret", "X_OAUTH2_CLIENT_SECRET"), "")
    if not client_id or not client_secret:
        raise ValueError(
            "OAuth 2.0 credentials missing. Set X_OAUTH2_CLIENT_ID and X_OAUTH2_CLIENT_SECRET "
            "in .env, then run: python bookmarks_auth.py"
        )
    return client_id, client_secret


def _load_oauth2_tokens() -> dict | None:
    """Load saved OAuth 2.0 tokens from disk."""
    if _OAUTH2_TOKEN_FILE.exists():
        try:
            import json
            return json.loads(_OAUTH2_TOKEN_FILE.read_text())
        except Exception:
            pass
    return None


def _save_oauth2_tokens(tokens: dict):
    """Persist OAuth 2.0 tokens to disk."""
    import json
    _OAUTH2_TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    _OAUTH2_TOKEN_FILE.write_text(json.dumps(tokens, indent=2))
    _OAUTH2_TOKEN_FILE.chmod(0o600)


def _refresh_oauth2_token() -> str:
    """Refresh the OAuth 2.0 access token using the stored refresh token.

    Returns the new access token, or raises if refresh fails.
    """
    tokens = _load_oauth2_tokens()
    if not tokens or "refresh_token" not in tokens:
        raise ValueError(
            "No OAuth 2.0 refresh token found. Run: python bookmarks_auth.py"
        )

    client_id, client_secret = _get_oauth2_creds()

    r = requests.post(
        _OAUTH2_TOKEN_URL,
        data={
            "grant_type": "refresh_token",
            "refresh_token": tokens["refresh_token"],
            "client_id": client_id,
        },
        auth=(client_id, client_secret),
        timeout=15,
    )

    if r.status_code != 200:
        log.error(f"OAuth 2.0 token refresh failed: {r.status_code} {r.text[:300]}")
        raise ValueError(f"Token refresh failed ({r.status_code}). Re-run: python bookmarks_auth.py")

    new_tokens = r.json()
    _save_oauth2_tokens(new_tokens)
    log.info("OAuth 2.0 token refreshed")
    return new_tokens["access_token"]


def _get_oauth2_bearer() -> str:
    """Get a valid OAuth 2.0 bearer token, refreshing if needed."""
    tokens = _load_oauth2_tokens()
    if not tokens:
        raise ValueError("No OAuth 2.0 tokens. Run: python bookmarks_auth.py")

    # Try the stored access token first
    access_token = tokens.get("access_token", "")
    if access_token:
        # Test if it's still valid with a lightweight call
        r = requests.get(
            f"{API_BASE}/users/me",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=10,
        )
        if r.status_code == 200:
            return access_token
        if r.status_code == 401:
            log.info("OAuth 2.0 access token expired, refreshing...")

    return _refresh_oauth2_token()


def get_bookmarks(max_results: int = 40) -> list[XPost]:
    """Fetch bookmarked tweets via X API v2 (requires OAuth 2.0).

    Returns list of XPost objects with image URLs.
    """
    bearer = _get_oauth2_bearer()
    user_id = _get_user_id()

    r = requests.get(
        f"{API_BASE}/users/{user_id}/bookmarks",
        params={
            "max_results": min(max_results, 100),
            "tweet.fields": "public_metrics,author_id,created_at,lang,attachments,text",
            "expansions": "author_id,attachments.media_keys",
            "media.fields": "url,type,preview_image_url",
            "user.fields": "username,name",
        },
        headers={"Authorization": f"Bearer {bearer}"},
        timeout=15,
    )

    if r.status_code == 429:
        log.warning("Rate limited on bookmarks")
        return []
    if r.status_code != 200:
        log.error(f"Bookmarks failed: {r.status_code} {r.text[:300]}")
        return []

    data = r.json()
    if "data" not in data:
        log.info("No bookmarks found")
        return []

    users = {u["id"]: u for u in data.get("includes", {}).get("users", [])}
    media_map = {m["media_key"]: m for m in data.get("includes", {}).get("media", [])}

    posts = []
    for tweet in data["data"]:
        user = users.get(tweet.get("author_id"), {})
        metrics = tweet.get("public_metrics", {})

        image_urls = []
        media_keys = tweet.get("attachments", {}).get("media_keys", [])
        for mk in media_keys:
            m = media_map.get(mk, {})
            if m.get("type") == "photo":
                url = m.get("url") or m.get("preview_image_url")
                if url:
                    image_urls.append(_to_orig_url(url))

        posts.append(XPost(
            post_id=tweet["id"],
            author_handle=user.get("username", ""),
            author_name=user.get("name", ""),
            author_id=tweet.get("author_id", ""),
            text=tweet.get("text", ""),
            image_urls=image_urls,
            likes=metrics.get("like_count", 0),
            reposts=metrics.get("retweet_count", 0),
            replies=metrics.get("reply_count", 0),
            views=metrics.get("impression_count", 0),
            language=tweet.get("lang"),
            created_at=tweet.get("created_at"),
        ))

    log.info(f"Fetched {len(posts)} bookmarks")
    return posts
