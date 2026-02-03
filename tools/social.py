"""
Social media posting tools.
Currently supports X (Twitter).
"""

import os
import httpx
import tweepy
from typing import Optional
from dataclasses import dataclass

# X/Twitter credentials
X_API_KEY = os.environ.get("X_API_KEY", "")
X_API_SECRET = os.environ.get("X_API_SECRET", "")
X_ACCESS_TOKEN = os.environ.get("X_ACCESS_TOKEN", "")
X_ACCESS_SECRET = os.environ.get("X_ACCESS_SECRET", "")
X_BEARER_TOKEN = os.environ.get("X_BEARER_TOKEN", "")


@dataclass
class PostResult:
    """Result of posting to social media."""
    success: bool
    platform: str
    post_id: Optional[str] = None
    post_url: Optional[str] = None
    error: Optional[str] = None


def get_x_client() -> Optional[tweepy.Client]:
    """Get authenticated X/Twitter API client."""
    if not all([X_API_KEY, X_API_SECRET, X_ACCESS_TOKEN, X_ACCESS_SECRET]):
        return None

    return tweepy.Client(
        consumer_key=X_API_KEY,
        consumer_secret=X_API_SECRET,
        access_token=X_ACCESS_TOKEN,
        access_token_secret=X_ACCESS_SECRET,
    )


def get_x_api_v1() -> Optional[tweepy.API]:
    """Get v1.1 API for media upload."""
    if not all([X_API_KEY, X_API_SECRET, X_ACCESS_TOKEN, X_ACCESS_SECRET]):
        return None

    auth = tweepy.OAuth1UserHandler(
        X_API_KEY, X_API_SECRET,
        X_ACCESS_TOKEN, X_ACCESS_SECRET,
    )
    return tweepy.API(auth)


async def download_image(image_url: str) -> Optional[bytes]:
    """Download an image from URL."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            response = await client.get(
                image_url,
                follow_redirects=True,
                headers={
                    "User-Agent": "Mozilla/5.0 (compatible; ContentCurator/1.0)"
                },
            )
            if response.status_code == 200:
                return response.content
            return None
        except Exception:
            return None


async def post_to_x(
    text: str,
    image_url: Optional[str] = None,
    image_data: Optional[bytes] = None,
) -> PostResult:
    """
    Post to X (Twitter) with optional image.

    Args:
        text: Tweet text
        image_url: URL of image to attach (will be downloaded)
        image_data: Raw image bytes (alternative to URL)

    Returns:
        PostResult with success status and post details
    """
    client = get_x_client()
    if not client:
        return PostResult(
            success=False,
            platform="x",
            error="X API credentials not configured",
        )

    try:
        media_id = None

        # Handle image if provided
        if image_url or image_data:
            api_v1 = get_x_api_v1()
            if not api_v1:
                return PostResult(
                    success=False,
                    platform="x",
                    error="X API v1.1 not configured for media upload",
                )

            # Download image if URL provided
            if image_url and not image_data:
                image_data = await download_image(image_url)
                if not image_data:
                    return PostResult(
                        success=False,
                        platform="x",
                        error=f"Failed to download image: {image_url}",
                    )

            # Upload media
            import tempfile
            with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as f:
                f.write(image_data)
                temp_path = f.name

            try:
                media = api_v1.media_upload(filename=temp_path)
                media_id = media.media_id
            finally:
                import os
                os.unlink(temp_path)

        # Create tweet
        if media_id:
            response = client.create_tweet(text=text, media_ids=[media_id])
        else:
            response = client.create_tweet(text=text)

        tweet_id = response.data["id"]

        return PostResult(
            success=True,
            platform="x",
            post_id=tweet_id,
            post_url=f"https://x.com/i/web/status/{tweet_id}",
        )

    except tweepy.TweepyException as e:
        return PostResult(
            success=False,
            platform="x",
            error=f"X API error: {str(e)}",
        )
    except Exception as e:
        return PostResult(
            success=False,
            platform="x",
            error=f"Unexpected error: {str(e)}",
        )


async def get_post_metrics(post_id: str) -> Optional[dict]:
    """
    Get engagement metrics for a post.

    Returns dict with: likes, reposts, replies, impressions
    """
    client = get_x_client()
    if not client:
        return None

    try:
        response = client.get_tweet(
            post_id,
            tweet_fields=["public_metrics", "created_at"],
        )

        if not response.data:
            return None

        metrics = response.data.public_metrics
        return {
            "likes": metrics.get("like_count", 0),
            "reposts": metrics.get("retweet_count", 0),
            "replies": metrics.get("reply_count", 0),
            "impressions": metrics.get("impression_count", 0),
            "quotes": metrics.get("quote_count", 0),
        }

    except Exception:
        return None


async def verify_credentials() -> dict:
    """
    Verify X API credentials and return account info.
    """
    client = get_x_client()
    if not client:
        return {"valid": False, "error": "Credentials not configured"}

    try:
        response = client.get_me(user_fields=["username", "name", "public_metrics"])

        if response.data:
            return {
                "valid": True,
                "username": response.data.username,
                "name": response.data.name,
                "followers": response.data.public_metrics.get("followers_count", 0),
            }
        return {"valid": False, "error": "Could not fetch user info"}

    except tweepy.TweepyException as e:
        return {"valid": False, "error": str(e)}


def format_tweet(caption: str, hashtags: Optional[list[str]] = None, max_length: int = 280) -> str:
    """
    Format caption with hashtags for X, respecting character limit.
    """
    text = caption.strip()

    if hashtags:
        # Add hashtags that fit
        hashtag_text = " " + " ".join(hashtags[:3])
        if len(text) + len(hashtag_text) <= max_length:
            text += hashtag_text

    # Truncate if still too long
    if len(text) > max_length:
        text = text[:max_length - 3] + "..."

    return text
