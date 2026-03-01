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
            # Convert to RGB if necessary (e.g. RGBA PNGs)
            if img.mode in ("RGBA", "P"):
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
                # Resize as last resort
                w, h = img.size
                ratio = (900_000 / len(path.read_bytes())) ** 0.5
                img = img.resize((int(w * ratio), int(h * ratio)))
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
