"""
Bluesky API module — niche-agnostic.
Mirrors tools/xapi.py pattern: set_niche() switches active account,
lazy-init client with session persistence per niche.

Uses atproto Python SDK for AT Protocol.
"""

import os
import re
import time
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import grapheme
from atproto import Client, SessionEvent, models

from config.niches import get_niche

log = logging.getLogger("bluesky")

BASE_DIR = Path(__file__).parent.parent
DATA_DIR = BASE_DIR / "data"

# Module-level state (same pattern as xapi.py)
_active_niche: str | None = None
_clients: dict[str, Client] = {}

# Profile cache: {did: (profile_dict, cached_at_timestamp)}
_profile_cache: dict[str, tuple[dict, float]] = {}
_PROFILE_CACHE_TTL = 3600  # 1 hour

# Rate budget tracker: caps at 4000 points/hr (leaves 1000 buffer from 5000 limit)
_rate_budget: dict[str, list] = {}  # {niche: [(timestamp, points), ...]}
_RATE_BUDGET_MAX = 4000
_RATE_BUDGET_WINDOW = 3600  # 1 hour


@dataclass
class BskyPost:
    """Bluesky post data, mirrors XPost pattern."""
    uri: str
    cid: str
    author_handle: str
    author_did: str
    text: str
    image_count: int
    likes: int
    reposts: int
    replies: int
    created_at: str
    author_followers: int = 0


def set_niche(niche_id: str | None):
    """Set the active niche for Bluesky credential resolution."""
    global _active_niche
    _active_niche = niche_id


def _session_file(niche_id: str) -> Path:
    return DATA_DIR / f"bluesky-session-{niche_id}.txt"


def _get_client() -> Client:
    """Get or create a Bluesky client for the active niche.

    Reuses existing authenticated clients. Restores sessions from disk
    to avoid hitting createSession rate limit (300/day).
    """
    if not _active_niche:
        raise RuntimeError("Call set_niche() before using Bluesky tools")

    # Return cached client if already authenticated
    if _active_niche in _clients:
        return _clients[_active_niche]

    niche = get_niche(_active_niche)
    bsky_env = niche.get("bluesky_env")
    if not bsky_env:
        raise RuntimeError(f"No bluesky_env configured for niche {_active_niche}")

    handle = os.environ.get(bsky_env["handle"])
    app_password = os.environ.get(bsky_env["app_password"])
    if not handle or not app_password:
        raise RuntimeError(
            f"Missing Bluesky credentials: {bsky_env['handle']} and/or {bsky_env['app_password']}"
        )

    client = Client()

    # Session change callback — persist session string to disk
    def on_session_change(event: SessionEvent, session) -> None:
        sf = _session_file(_active_niche)
        if event in (SessionEvent.CREATE, SessionEvent.REFRESH):
            sf.write_text(client.export_session_string())
            log.debug(f"Session saved for {_active_niche}")

    client.on_session_change(on_session_change)

    # Try restoring session from disk first
    sf = _session_file(_active_niche)
    restored = False
    if sf.exists():
        try:
            session_str = sf.read_text().strip()
            if session_str:
                client.login(session_string=session_str)
                log.info(f"Restored Bluesky session for {_active_niche}")
                restored = True
        except Exception as e:
            log.warning(f"Session restore failed for {_active_niche}, will re-login: {e}")

    if not restored:
        client.login(handle, app_password)
        log.info(f"Logged into Bluesky as {handle}")

    _clients[_active_niche] = client
    return client


def count_graphemes(text: str) -> int:
    """Count grapheme clusters in text (Bluesky limit unit)."""
    return grapheme.length(text)


def _split_text(text: str, limit: int = 300) -> list[str]:
    """Split text into chunks that fit within the grapheme limit.

    Splits at sentence boundaries first, then at word boundaries.
    Returns list of text chunks.
    """
    if count_graphemes(text) <= limit:
        return [text]

    # Split into sentences
    sentences = re.split(r'(?<=[.!?])\s+', text)

    chunks = []
    current = ""

    for sentence in sentences:
        test = f"{current} {sentence}".strip() if current else sentence
        if count_graphemes(test) <= limit:
            current = test
        else:
            if current:
                chunks.append(current)
            # If single sentence is over limit, split at word boundary
            if count_graphemes(sentence) > limit:
                words = sentence.split()
                current = ""
                for word in words:
                    test_word = f"{current} {word}".strip() if current else word
                    if count_graphemes(test_word) <= limit:
                        current = test_word
                    else:
                        if current:
                            chunks.append(current)
                        current = word
            else:
                current = sentence

    if current:
        chunks.append(current)

    return chunks


def upload_image(file_path: str, alt_text: str = "") -> models.AppBskyEmbedImages.Image | None:
    """Upload an image to Bluesky and return an Image model.

    Compresses to <1MB if needed (Bluesky limit).
    """
    client = _get_client()
    path = Path(file_path)

    if not path.exists():
        log.warning(f"Image not found: {file_path}")
        return None

    img_data = path.read_bytes()

    # Compress if over 1MB
    if len(img_data) > 1_000_000:
        try:
            from PIL import Image as PILImage
            import io

            img = PILImage.open(path)
            # Convert to RGB if necessary (RGBA, P, L, LA, CMYK, etc.)
            if img.mode != "RGB":
                img = img.convert("RGB")

            quality = 85
            while quality >= 30:
                buf = io.BytesIO()
                img.save(buf, format="JPEG", quality=quality)
                img_data = buf.getvalue()
                if len(img_data) <= 1_000_000:
                    break
                quality -= 10

            if len(img_data) > 1_000_000:
                # Resize as last resort — use current compressed size for ratio
                w, h = img.size
                ratio = (900_000 / len(img_data)) ** 0.5
                new_w, new_h = max(1, int(w * ratio)), max(1, int(h * ratio))
                img = img.resize((new_w, new_h))
                buf = io.BytesIO()
                img.save(buf, format="JPEG", quality=75)
                img_data = buf.getvalue()

            log.info(f"Compressed {path.name}: {len(img_data) / 1024:.0f}KB")
        except Exception as e:
            log.warning(f"Image compression failed for {path.name}: {e}")
            return None

    try:
        blob = client.upload_blob(img_data)
        return models.AppBskyEmbedImages.Image(
            alt=alt_text or "",
            image=blob.blob,
        )
    except Exception as e:
        log.error(f"Failed to upload image {path.name}: {e}")
        return None


def create_post(text: str, image_paths: list[str] | None = None,
                alt_texts: list[str] | None = None) -> str | None:
    """Create a single Bluesky post with optional images.

    If text > 300 graphemes, automatically splits into a thread.
    Returns the post URI, or None on failure.
    """
    client = _get_client()

    # Handle text splitting
    chunks = _split_text(text)
    if len(chunks) > 1:
        # Build thread posts
        thread_posts = [{"text": chunks[0], "image_paths": image_paths or [], "alt_texts": alt_texts or []}]
        for chunk in chunks[1:]:
            thread_posts.append({"text": chunk, "image_paths": [], "alt_texts": []})
        return post_thread(thread_posts)

    # Single post
    embed = None
    if image_paths:
        images = []
        alts = alt_texts or [""] * len(image_paths)
        for i, img_path in enumerate(image_paths[:4]):  # Bluesky max 4 images
            alt = alts[i] if i < len(alts) else ""
            img_model = upload_image(img_path, alt_text=alt)
            if img_model:
                images.append(img_model)

        if images:
            embed = models.AppBskyEmbedImages.Main(images=images)

    try:
        resp = client.send_post(text=text, embed=embed)
        log.info(f"Posted to Bluesky: {resp.uri}")
        return resp.uri
    except Exception as e:
        log.error(f"Bluesky post failed: {e}")
        return None


def post_thread(posts: list[dict]) -> list[str] | None:
    """Post a thread (multiple chained posts) to Bluesky.

    Each post dict: {text: str, image_paths: list[str], alt_texts: list[str]}
    Returns list of post URIs, or None on failure.
    """
    client = _get_client()
    uris = []
    root_ref = None
    parent_ref = None

    for i, post_data in enumerate(posts):
        text = post_data.get("text", "")
        image_paths = post_data.get("image_paths", [])
        alt_texts = post_data.get("alt_texts", [])

        # Truncate if still over limit after split
        if count_graphemes(text) > 300:
            text = grapheme.slice(text, 0, 297) + "..."

        # Build image embed
        embed = None
        if image_paths:
            images = []
            for j, img_path in enumerate(image_paths[:4]):
                alt = alt_texts[j] if j < len(alt_texts) else ""
                img_model = upload_image(img_path, alt_text=alt)
                if img_model:
                    images.append(img_model)
            if images:
                embed = models.AppBskyEmbedImages.Main(images=images)

        # Build reply ref for thread chaining
        reply_to = None
        if parent_ref:
            reply_to = models.AppBskyFeedPost.ReplyRef(
                root=root_ref,
                parent=parent_ref,
            )

        try:
            resp = client.send_post(text=text, embed=embed, reply_to=reply_to)
            uris.append(resp.uri)
            log.info(f"  Thread post {i + 1}/{len(posts)}: {resp.uri}")

            # Create strong ref for threading
            strong_ref = models.create_strong_ref(resp)
            if i == 0:
                root_ref = strong_ref
            parent_ref = strong_ref

            # Small delay between thread posts
            if i < len(posts) - 1:
                time.sleep(1)

        except Exception as e:
            log.error(f"Thread post {i + 1} failed: {e}")
            break

    return uris if uris else None


# ---------------------------------------------------------------------------
# Rate budget helpers
# ---------------------------------------------------------------------------

def _track_rate(points: int = 1):
    """Track API call cost against hourly budget."""
    niche = _active_niche or "_default"
    now = time.time()
    if niche not in _rate_budget:
        _rate_budget[niche] = []
    # Prune old entries
    _rate_budget[niche] = [(t, p) for t, p in _rate_budget[niche] if now - t < _RATE_BUDGET_WINDOW]
    _rate_budget[niche].append((now, points))


def rate_budget_remaining() -> int:
    """Return approximate remaining rate points for this hour."""
    niche = _active_niche or "_default"
    now = time.time()
    if niche not in _rate_budget:
        return _RATE_BUDGET_MAX
    recent = [(t, p) for t, p in _rate_budget[niche] if now - t < _RATE_BUDGET_WINDOW]
    used = sum(p for _, p in recent)
    return max(0, _RATE_BUDGET_MAX - used)


# ---------------------------------------------------------------------------
# Profile cache
# ---------------------------------------------------------------------------

def get_profile(handle_or_did: str) -> dict | None:
    """Get a Bluesky profile. Cached for 1 hour."""
    now = time.time()
    if handle_or_did in _profile_cache:
        profile, cached_at = _profile_cache[handle_or_did]
        if now - cached_at < _PROFILE_CACHE_TTL:
            return profile

    try:
        client = _get_client()
        _track_rate(1)
        resp = client.app.bsky.actor.get_profile({"actor": handle_or_did})
        profile = {
            "did": resp.did,
            "handle": resp.handle,
            "display_name": resp.display_name or "",
            "followers_count": resp.followers_count or 0,
            "follows_count": resp.follows_count or 0,
            "posts_count": resp.posts_count or 0,
        }
        _profile_cache[handle_or_did] = (profile, now)
        # Also cache by DID if we looked up by handle
        if handle_or_did != resp.did:
            _profile_cache[resp.did] = (profile, now)
        return profile
    except Exception as e:
        log.warning(f"Failed to get profile for {handle_or_did}: {e}")
        return None


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

def _clean_query_for_bluesky(query: str) -> str:
    """Strip X-specific operators from search queries for Bluesky."""
    # Remove has:images, has:media, has:links
    query = re.sub(r'\bhas:\w+\b', '', query)
    # Remove -is:retweet, is:reply etc
    query = re.sub(r'-?is:\w+', '', query)
    # Remove filter:X operators
    query = re.sub(r'-?filter:\w+', '', query)
    # Remove lang:xx (Bluesky uses a separate parameter)
    query = re.sub(r'\blang:\w+\b', '', query)
    # Remove min_faves:N etc
    query = re.sub(r'\bmin_\w+:\d+\b', '', query)
    # Collapse whitespace
    query = re.sub(r'\s+', ' ', query).strip()
    return query


def search_posts(query: str, limit: int = 25, sort: str = "top", lang: str | None = None) -> list[BskyPost]:
    """Search Bluesky posts. Returns list of BskyPost."""
    if rate_budget_remaining() < 10:
        log.warning("Rate budget nearly exhausted, skipping search")
        return []

    client = _get_client()
    cleaned = _clean_query_for_bluesky(query)
    if not cleaned:
        log.warning(f"Query empty after cleaning: {query}")
        return []

    try:
        _track_rate(5)  # search is heavier
        params = {"q": cleaned, "limit": min(limit, 25), "sort": sort}
        if lang:
            params["lang"] = lang
        resp = client.app.bsky.feed.search_posts(params)
    except Exception as e:
        log.error(f"Bluesky search failed for '{cleaned}': {e}")
        return []

    posts = []
    for item in resp.posts or []:
        try:
            record = item.record
            author = item.author
            # Count images
            img_count = 0
            if hasattr(record, 'embed') and record.embed:
                if hasattr(record.embed, 'images'):
                    img_count = len(record.embed.images or [])

            # Get follower count from cache or fetch
            followers = 0
            cached = _profile_cache.get(author.did)
            if cached and time.time() - cached[1] < _PROFILE_CACHE_TTL:
                followers = cached[0].get("followers_count", 0)
            elif author.did not in _profile_cache:
                # Batch-friendly: cache the handle->profile mapping
                _profile_cache[author.did] = ({
                    "did": author.did,
                    "handle": author.handle,
                    "display_name": author.display_name or "",
                    "followers_count": 0,
                }, time.time())

            posts.append(BskyPost(
                uri=item.uri,
                cid=item.cid,
                author_handle=author.handle,
                author_did=author.did,
                text=record.text if hasattr(record, 'text') else "",
                image_count=img_count,
                likes=item.like_count or 0,
                reposts=item.repost_count or 0,
                replies=item.reply_count or 0,
                created_at=record.created_at if hasattr(record, 'created_at') else "",
                author_followers=followers,
            ))
        except Exception as e:
            log.debug(f"Error parsing search result: {e}")
            continue

    return posts


# ---------------------------------------------------------------------------
# Engagement actions
# ---------------------------------------------------------------------------

def like_post(uri: str, cid: str) -> str | None:
    """Like a post. Returns like URI or None."""
    try:
        client = _get_client()
        _track_rate(3)
        resp = client.like(uri=uri, cid=cid)
        log.debug(f"Liked {uri}")
        return resp.uri
    except Exception as e:
        log.error(f"Failed to like {uri}: {e}")
        return None


def follow_user(did: str) -> str | None:
    """Follow a user by DID. Returns follow URI or None."""
    try:
        client = _get_client()
        _track_rate(3)
        resp = client.follow(did)
        log.debug(f"Followed {did}")
        return resp.uri
    except Exception as e:
        log.error(f"Failed to follow {did}: {e}")
        return None


def reply_to_post(parent_uri: str, parent_cid: str, root_uri: str, root_cid: str, text: str) -> str | None:
    """Reply to a post. Returns reply URI or None."""
    try:
        client = _get_client()
        _track_rate(3)

        parent_ref = models.ComAtprotoRepoStrongRef.Main(uri=parent_uri, cid=parent_cid)
        root_ref = models.ComAtprotoRepoStrongRef.Main(uri=root_uri, cid=root_cid)

        reply_ref = models.AppBskyFeedPost.ReplyRef(root=root_ref, parent=parent_ref)
        resp = client.send_post(text=text, reply_to=reply_ref)
        log.info(f"Replied to {parent_uri}: {text[:60]}...")
        return resp.uri
    except Exception as e:
        log.error(f"Failed to reply to {parent_uri}: {e}")
        return None


def repost(uri: str, cid: str) -> str | None:
    """Repost a post. Returns repost URI or None."""
    try:
        client = _get_client()
        _track_rate(3)
        resp = client.repost(uri=uri, cid=cid)
        log.debug(f"Reposted {uri}")
        return resp.uri
    except Exception as e:
        log.error(f"Failed to repost {uri}: {e}")
        return None


# ---------------------------------------------------------------------------
# Notifications & feed reading
# ---------------------------------------------------------------------------

def get_notifications(limit: int = 50, reasons: list[str] | None = None) -> list[dict]:
    """Get recent notifications. Filter by reasons (reply, mention, like, repost, follow)."""
    if reasons is None:
        reasons = ["reply", "mention"]
    try:
        client = _get_client()
        _track_rate(3)
        resp = client.app.bsky.notification.list_notifications({"limit": limit})
        results = []
        for notif in resp.notifications or []:
            if notif.reason not in reasons:
                continue
            record = notif.record
            results.append({
                "uri": notif.uri,
                "cid": notif.cid,
                "reason": notif.reason,
                "author_handle": notif.author.handle,
                "author_did": notif.author.did,
                "text": record.text if hasattr(record, 'text') else "",
                "indexed_at": notif.indexed_at,
                "is_read": notif.is_read,
                # For replies, the record has a reply ref
                "parent_uri": None,
                "parent_cid": None,
                "root_uri": None,
                "root_cid": None,
            })
            # Extract reply refs if present
            if hasattr(record, 'reply') and record.reply:
                results[-1]["parent_uri"] = record.reply.parent.uri if record.reply.parent else None
                results[-1]["parent_cid"] = record.reply.parent.cid if record.reply.parent else None
                results[-1]["root_uri"] = record.reply.root.uri if record.reply.root else None
                results[-1]["root_cid"] = record.reply.root.cid if record.reply.root else None
        return results
    except Exception as e:
        log.error(f"Failed to get notifications: {e}")
        return []


def get_post_thread(uri: str, depth: int = 1) -> dict | None:
    """Get a post and its thread context. Returns simplified dict."""
    try:
        client = _get_client()
        _track_rate(3)
        resp = client.app.bsky.feed.get_post_thread({"uri": uri, "depth": depth})
        thread = resp.thread
        if not thread or not hasattr(thread, 'post'):
            return None
        post = thread.post
        record = post.record
        result = {
            "uri": post.uri,
            "cid": post.cid,
            "text": record.text if hasattr(record, 'text') else "",
            "author_handle": post.author.handle,
            "author_did": post.author.did,
            "likes": post.like_count or 0,
            "reposts": post.repost_count or 0,
            "replies": post.reply_count or 0,
        }
        # Include parent if available
        if hasattr(thread, 'parent') and thread.parent and hasattr(thread.parent, 'post'):
            pp = thread.parent.post
            pr = pp.record
            result["parent"] = {
                "uri": pp.uri,
                "cid": pp.cid,
                "text": pr.text if hasattr(pr, 'text') else "",
                "author_handle": pp.author.handle,
            }
        return result
    except Exception as e:
        log.warning(f"Failed to get post thread for {uri}: {e}")
        return None


def get_own_posts(limit: int = 20) -> list[BskyPost]:
    """Get our own recent posts."""
    try:
        client = _get_client()
        _track_rate(3)
        # Get the DID of the logged-in user
        did = client.me.did
        resp = client.app.bsky.feed.get_author_feed({"actor": did, "limit": limit})
        posts = []
        for item in resp.feed or []:
            post = item.post
            record = post.record
            img_count = 0
            if hasattr(record, 'embed') and record.embed and hasattr(record.embed, 'images'):
                img_count = len(record.embed.images or [])
            posts.append(BskyPost(
                uri=post.uri,
                cid=post.cid,
                author_handle=post.author.handle,
                author_did=post.author.did,
                text=record.text if hasattr(record, 'text') else "",
                image_count=img_count,
                likes=post.like_count or 0,
                reposts=post.repost_count or 0,
                replies=post.reply_count or 0,
                created_at=record.created_at if hasattr(record, 'created_at') else "",
            ))
        return posts
    except Exception as e:
        log.error(f"Failed to get own posts: {e}")
        return []
