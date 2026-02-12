"""
X/Twitter operations via Twikit (no API key needed).
Wraps twikit for our specific use cases.
Niche-agnostic — takes account credentials as config.
"""

import os
import json
import asyncio
import logging
from pathlib import Path
from typing import Optional
from dataclasses import dataclass

import httpx
from twikit import Client

logger = logging.getLogger(__name__)

# Session cookies directory
COOKIES_DIR = Path(os.environ.get("COOKIES_DIR", "data/cookies"))


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


@dataclass
class XUser:
    """An X user profile."""
    user_id: str
    handle: str
    name: str
    follower_count: int
    following_count: int
    description: str


# Cache logged-in clients per niche to avoid repeated logins
_clients: dict[str, Client] = {}


def _cookies_path(niche_id: str) -> Path:
    """Get the cookies file path for a niche account."""
    COOKIES_DIR.mkdir(parents=True, exist_ok=True)
    return COOKIES_DIR / f"{niche_id}_cookies.json"


async def login(niche_id: str) -> Client:
    """
    Login to X for a niche account and cache the session.
    Credentials come from environment variables keyed by niche.

    Env vars:
        X_USERNAME_{NICHE} — X username
        X_EMAIL_{NICHE} — X email
        X_PASSWORD_{NICHE} — X password

    Falls back to X_USERNAME, X_EMAIL, X_PASSWORD if niche-specific not set.
    """
    if niche_id in _clients:
        return _clients[niche_id]

    niche_upper = niche_id.upper()
    username = os.environ.get(f"X_USERNAME_{niche_upper}", os.environ.get("X_USERNAME", ""))
    email = os.environ.get(f"X_EMAIL_{niche_upper}", os.environ.get("X_EMAIL", ""))
    password = os.environ.get(f"X_PASSWORD_{niche_upper}", os.environ.get("X_PASSWORD", ""))

    if not all([username, email, password]):
        raise ValueError(
            f"X credentials not configured for niche '{niche_id}'. "
            f"Set X_USERNAME_{niche_upper}, X_EMAIL_{niche_upper}, X_PASSWORD_{niche_upper} "
            f"or X_USERNAME, X_EMAIL, X_PASSWORD"
        )

    client = Client("en-US")
    cookies_file = str(_cookies_path(niche_id))

    # Try loading existing cookies first
    if Path(cookies_file).exists():
        try:
            # Try twikit's native format first
            client.load_cookies(cookies_file)
            # Verify session with a lightweight call (account/settings.json is 404 now)
            await client.get_user_by_screen_name(username)
            logger.info(f"Loaded existing session for {niche_id}")
            _clients[niche_id] = client
            return client
        except Exception:
            # Try Chrome-exported format (simple {name: value} dict)
            try:
                cookie_data = json.loads(Path(cookies_file).read_text())
                if isinstance(cookie_data, dict) and "auth_token" in cookie_data:
                    client.set_cookies(cookie_data)
                    await client.get_user_by_screen_name(username)
                    logger.info(f"Loaded Chrome cookies for {niche_id}")
                    _clients[niche_id] = client
                    return client
            except Exception:
                pass
            logger.warning(f"Saved session expired for {niche_id}")

    # Fresh login — usually blocked by Cloudflare, but try as last resort
    if not all([username, email, password]):
        raise ValueError(
            f"No valid cookies and no credentials for '{niche_id}'. "
            f"Re-export cookies from Chrome using pycookiecheat."
        )
    logger.info(f"Attempting fresh login for {niche_id} (may be blocked)...")
    await client.login(
        auth_info_1=username,
        auth_info_2=email,
        password=password,
    )
    client.save_cookies(cookies_file)
    logger.info(f"Logged in to X as {username} for niche {niche_id}")

    _clients[niche_id] = client
    return client


async def search_posts(
    client: Client,
    query: str,
    count: int = 20,
    product: str = "Latest",
) -> list[XPost]:
    """
    Search X for posts matching query.

    Args:
        client: Authenticated twikit client
        query: Search query (supports X search operators)
        count: Number of results to fetch
        product: 'Latest', 'Top', or 'Media'

    Returns:
        List of XPost objects
    """
    max_retries = 3
    last_error = None
    for attempt in range(max_retries):
        try:
            results = await client.search_tweet(query, product, count=count)
            break
        except Exception as e:
            last_error = e
            if attempt < max_retries - 1:
                wait = (attempt + 1) * 5
                logger.warning(f"Search attempt {attempt + 1} failed ({e}), retrying in {wait}s...")
                await asyncio.sleep(wait)
            else:
                logger.error(f"Search failed after {max_retries} attempts for query '{query}': {e}")
                return []

    try:
        posts = []
        for tweet in results:
            images = []
            if hasattr(tweet, 'media') and tweet.media:
                for media in tweet.media:
                    if hasattr(media, 'media_url_https'):
                        images.append(media.media_url_https)
                    elif hasattr(media, 'media_url'):
                        images.append(media.media_url)
                    elif isinstance(media, dict) and 'media_url_https' in media:
                        images.append(media['media_url_https'])
                    elif isinstance(media, dict) and 'media_url' in media:
                        images.append(media['media_url'])

            posts.append(XPost(
                post_id=tweet.id,
                author_handle=tweet.user.screen_name if tweet.user else "",
                author_name=tweet.user.name if tweet.user else "",
                author_id=tweet.user.id if tweet.user else "",
                text=tweet.text or "",
                image_urls=images,
                likes=tweet.favorite_count or 0,
                reposts=tweet.retweet_count or 0,
                replies=tweet.reply_count or 0,
                views=tweet.view_count or 0,
                language=getattr(tweet, 'lang', None),
                created_at=str(tweet.created_at) if hasattr(tweet, 'created_at') else None,
            ))
        return posts
    except Exception as e:
        logger.error(f"Search failed for query '{query}': {e}")
        return []


async def get_user_posts(
    client: Client,
    handle: str,
    count: int = 20,
) -> list[XPost]:
    """
    Get recent posts from a user by their handle.

    Args:
        client: Authenticated twikit client
        handle: User's screen name (without @)
        count: Number of posts to fetch

    Returns:
        List of XPost objects
    """
    try:
        # Get user ID from handle
        handle_clean = handle.lstrip("@")
        user = await client.get_user_by_screen_name(handle_clean)

        tweets = await client.get_user_tweets(user.id, "Tweets", count=count)
        posts = []
        for tweet in tweets:
            images = []
            if hasattr(tweet, 'media') and tweet.media:
                for media in tweet.media:
                    if hasattr(media, 'media_url_https'):
                        images.append(media.media_url_https)
                    elif hasattr(media, 'media_url'):
                        images.append(media.media_url)
                    elif isinstance(media, dict) and 'media_url_https' in media:
                        images.append(media['media_url_https'])
                    elif isinstance(media, dict) and 'media_url' in media:
                        images.append(media['media_url'])

            posts.append(XPost(
                post_id=tweet.id,
                author_handle=handle_clean,
                author_name=user.name,
                author_id=user.id,
                text=tweet.text or "",
                image_urls=images,
                likes=tweet.favorite_count or 0,
                reposts=tweet.retweet_count or 0,
                replies=tweet.reply_count or 0,
                views=tweet.view_count or 0,
                language=getattr(tweet, 'lang', None),
                created_at=str(tweet.created_at) if hasattr(tweet, 'created_at') else None,
            ))
        return posts
    except Exception as e:
        logger.error(f"Failed to get posts for @{handle}: {e}")
        return []


async def get_user_info(client: Client, handle: str) -> Optional[XUser]:
    """Get user profile info by handle."""
    try:
        handle_clean = handle.lstrip("@")
        user = await client.get_user_by_screen_name(handle_clean)
        return XUser(
            user_id=user.id,
            handle=user.screen_name,
            name=user.name,
            follower_count=user.followers_count or 0,
            following_count=user.following_count or 0,
            description=user.description or "",
        )
    except Exception as e:
        logger.error(f"Failed to get user info for @{handle}: {e}")
        return None


async def post_tweet(
    client: Client,
    text: str,
    image_paths: Optional[list[str]] = None,
    reply_to: Optional[str] = None,
    community_id: Optional[str] = None,
) -> Optional[str]:
    """
    Post a tweet with optional images, reply chaining, and community targeting.

    Args:
        client: Authenticated twikit client
        text: Tweet text
        image_paths: List of local file paths to attach
        reply_to: Tweet ID to reply to (for threads)
        community_id: X Community ID to post into

    Returns:
        Post ID string, or None on failure
    """
    try:
        media_ids = []
        if image_paths:
            for path in image_paths:
                media_id = await client.upload_media(path, wait_for_completion=True)
                media_ids.append(media_id)

        kwargs = {
            "text": text,
            "media_ids": media_ids if media_ids else None,
        }
        if reply_to:
            kwargs["reply_to"] = reply_to
        if community_id:
            kwargs["community_id"] = community_id

        tweet = await client.create_tweet(**kwargs)
        logger.info(f"Posted tweet: {tweet.id}")
        return tweet.id
    except Exception as e:
        logger.error(f"Failed to post tweet: {e}")
        return None


async def reply_to_post(
    client: Client,
    post_id: str,
    text: str,
) -> Optional[str]:
    """
    Reply to an existing post.

    Args:
        client: Authenticated twikit client
        post_id: ID of the post to reply to
        text: Reply text

    Returns:
        Reply post ID, or None on failure
    """
    try:
        tweet = await client.create_tweet(
            text=text,
            reply_to=post_id,
        )
        logger.info(f"Replied to {post_id}: {tweet.id}")
        return tweet.id
    except Exception as e:
        logger.error(f"Failed to reply to {post_id}: {e}")
        return None


async def post_thread(
    client: Client,
    tweets: list[str],
    image_paths: Optional[list[str]] = None,
    image_paths_per_tweet: Optional[list[Optional[list[str]]]] = None,
    community_id: Optional[str] = None,
    delay_range: tuple[int, int] = (120, 300),
) -> list[str]:
    """
    Post a thread (chain of tweets replying to each other).

    Args:
        client: Authenticated twikit client
        tweets: List of tweet texts in order
        image_paths: Images to attach to the FIRST tweet only (legacy)
        image_paths_per_tweet: Per-tweet image lists. Index i = images for tweet i.
            Takes priority over image_paths if provided.
        community_id: X Community ID to post into
        delay_range: (min, max) seconds to wait between tweets to avoid error 226

    Returns:
        List of tweet IDs posted (may be shorter than tweets if some failed)
    """
    import random

    if not tweets:
        return []

    # Build per-tweet image mapping
    if image_paths_per_tweet:
        imgs_per = image_paths_per_tweet
    elif image_paths:
        # Legacy: all images on first tweet only
        imgs_per = [image_paths] + [None] * (len(tweets) - 1)
    else:
        imgs_per = [None] * len(tweets)

    posted_ids = []

    # Post first tweet
    first_id = await post_tweet(
        client, tweets[0],
        image_paths=imgs_per[0] if len(imgs_per) > 0 else None,
        community_id=community_id,
    )
    if not first_id:
        logger.error("Thread failed: couldn't post first tweet")
        return []
    posted_ids.append(first_id)
    logger.info(f"Thread 1/{len(tweets)}: {first_id}")

    # Post remaining tweets as replies with delays
    for i, text in enumerate(tweets[1:], start=2):
        delay = random.uniform(*delay_range)
        logger.info(f"Waiting {delay:.0f}s before tweet {i}/{len(tweets)}...")
        await asyncio.sleep(delay)

        tweet_imgs = imgs_per[i - 1] if i - 1 < len(imgs_per) else None
        tweet_id = await post_tweet(
            client, text,
            image_paths=tweet_imgs,
            reply_to=posted_ids[-1],
        )
        if tweet_id:
            posted_ids.append(tweet_id)
            logger.info(f"Thread {i}/{len(tweets)}: {tweet_id}")
        else:
            logger.error(f"Thread broken at tweet {i}/{len(tweets)}")
            break

    return posted_ids


async def like_post(client: Client, post_id: str) -> bool:
    """Like a post. Returns True on success."""
    try:
        await client.favorite_tweet(post_id)
        logger.info(f"Liked post {post_id}")
        return True
    except Exception as e:
        logger.error(f"Failed to like {post_id}: {e}")
        return False


async def repost(client: Client, post_id: str) -> bool:
    """Repost/retweet a post. Returns True on success."""
    try:
        await client.retweet(post_id)
        logger.info(f"Reposted {post_id}")
        return True
    except Exception as e:
        logger.error(f"Failed to repost {post_id}: {e}")
        return False


async def follow_user(client: Client, handle: str) -> bool:
    """Follow a user by handle. Returns True on success."""
    try:
        handle_clean = handle.lstrip("@")
        user = await client.get_user_by_screen_name(handle_clean)
        await client.follow_user(user.id)
        logger.info(f"Followed @{handle_clean}")
        return True
    except Exception as e:
        logger.error(f"Failed to follow @{handle}: {e}")
        return False


async def download_image(url: str, save_dir: str = "data/images") -> Optional[str]:
    """
    Download image from URL to local path.

    Args:
        url: Image URL to download
        save_dir: Directory to save images

    Returns:
        Local file path, or None on failure
    """
    Path(save_dir).mkdir(parents=True, exist_ok=True)

    # Generate filename from URL
    import hashlib
    ext = ".jpg"
    if ".png" in url:
        ext = ".png"
    elif ".webp" in url:
        ext = ".webp"
    filename = hashlib.md5(url.encode()).hexdigest() + ext
    save_path = str(Path(save_dir) / filename)

    # Skip if already downloaded
    if Path(save_path).exists():
        return save_path

    # Upgrade Twitter image URLs to original resolution
    if "pbs.twimg.com" in url and "name=orig" not in url:
        base = url.split("?")[0]
        if base.endswith((".jpg", ".jpeg", ".png", ".webp")):
            ext = base.rsplit(".", 1)[-1]
            base = base.rsplit(".", 1)[0]
            url = f"{base}?format={ext}&name=orig"

    try:
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as http:
            resp = await http.get(url, headers={
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
            })
            if resp.status_code == 200 and len(resp.content) > 10_000:
                with open(save_path, "wb") as f:
                    f.write(resp.content)
                return save_path
    except Exception as e:
        logger.error(f"Failed to download image {url}: {e}")
    return None
