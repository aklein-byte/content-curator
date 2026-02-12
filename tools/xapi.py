"""
X/Twitter Official API v2 client.

Uses OAuth 1.0a (user context) for search, like, reply, follow.
Replaces twikit for engagement — more reliable, no cookie expiry, no 404 flakiness.

Requires in .env:
  X_API_CONSUMER_KEY, X_API_CONSUMER_SECRET,
  X_API_ACCESS_TOKEN, X_API_ACCESS_TOKEN_SECRET
"""

import os
import logging
import time
import requests
from requests_oauthlib import OAuth1
from dataclasses import dataclass
from typing import Optional

log = logging.getLogger(__name__)

API_BASE = "https://api.twitter.com/2"


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


def _get_auth() -> OAuth1:
    return OAuth1(
        os.environ["X_API_CONSUMER_KEY"],
        os.environ["X_API_CONSUMER_SECRET"],
        os.environ["X_API_ACCESS_TOKEN"],
        os.environ["X_API_ACCESS_TOKEN_SECRET"],
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
    """A post from X with metadata. Same shape as xkit.XPost for compatibility."""
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
            "user.fields": "username,name",
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


def create_tweet(text: str, media_ids: list[str] | None = None, reply_to: str | None = None) -> str | None:
    """Create a tweet via v2 API. Returns tweet ID or None."""
    payload = {"text": text}
    if media_ids:
        payload["media"] = {"media_ids": media_ids}
    if reply_to:
        payload["reply"] = {"in_reply_to_tweet_id": reply_to}

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
