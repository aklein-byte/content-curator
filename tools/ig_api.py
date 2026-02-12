"""
Instagram Graph API client for @tatamispaces.

Handles single image and carousel publishing via the official API.
Requires IG_GRAPH_TOKEN (long-lived token) and IG_USER_ID in .env.

API docs: https://developers.facebook.com/docs/instagram-platform/instagram-api-with-instagram-login/content-publishing
"""

import os
import time
import logging
import requests
from pathlib import Path
from datetime import datetime, timezone

log = logging.getLogger(__name__)

GRAPH_API_BASE = "https://graph.instagram.com/v22.0"


def get_ig_credentials() -> tuple[str, str]:
    """Load IG Graph API credentials from environment."""
    token = os.environ.get("IG_GRAPH_TOKEN")
    user_id = os.environ.get("IG_USER_ID")
    if not token or not user_id:
        raise RuntimeError(
            "Missing IG_GRAPH_TOKEN or IG_USER_ID in .env. "
            "See plan prerequisites for setup instructions."
        )
    return token, user_id


def check_token_info(token: str) -> dict:
    """Check token validity and expiry info."""
    resp = requests.get(
        "https://graph.instagram.com/me",
        params={"fields": "user_id,username", "access_token": token},
        timeout=15,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"Token check failed: {resp.status_code} {resp.text}")
    return resp.json()


def refresh_token_if_needed(token: str, env_path: Path | None = None) -> str:
    """Refresh a long-lived token if it's within 10 days of expiry.

    Long-lived IG tokens last 60 days and can be refreshed when they're
    at least 24 hours old. We refresh proactively when within 10 days.

    Returns the (possibly new) token.
    """
    # Check current token debug info
    resp = requests.get(
        "https://graph.instagram.com/refresh_access_token",
        params={
            "grant_type": "ig_refresh_token",
            "access_token": token,
        },
        timeout=15,
    )
    if resp.status_code == 200:
        data = resp.json()
        new_token = data.get("access_token", token)
        expires_in = data.get("expires_in", 0)
        days_left = expires_in // 86400
        log.info(f"Token refreshed. Expires in {days_left} days.")

        if new_token != token and env_path:
            _update_env_token(env_path, new_token)

        return new_token
    else:
        log.warning(f"Token refresh failed: {resp.status_code} {resp.text}")
        return token


def _update_env_token(env_path: Path, new_token: str):
    """Update IG_GRAPH_TOKEN in .env file."""
    content = env_path.read_text()
    lines = content.split("\n")
    updated = False
    for i, line in enumerate(lines):
        if line.startswith("IG_GRAPH_TOKEN="):
            lines[i] = f"IG_GRAPH_TOKEN={new_token}"
            updated = True
            break
    if updated:
        env_path.write_text("\n".join(lines))
        log.info("Updated IG_GRAPH_TOKEN in .env")


def _check_container_status(container_id: str, token: str, max_wait: int = 60) -> str:
    """Poll a media container until it's FINISHED or fails.

    Returns the status code ('FINISHED', 'ERROR', etc).
    """
    for attempt in range(max_wait // 2):
        resp = requests.get(
            f"{GRAPH_API_BASE}/{container_id}",
            params={"fields": "status_code,status", "access_token": token},
            timeout=15,
        )
        if resp.status_code != 200:
            log.warning(f"Container status check failed: {resp.text}")
            time.sleep(2)
            continue

        data = resp.json()
        status = data.get("status_code", "UNKNOWN")
        if status == "FINISHED":
            return status
        if status == "ERROR":
            error_msg = data.get("status", "Unknown error")
            raise RuntimeError(f"Container {container_id} failed: {error_msg}")
        if status in ("EXPIRED", "PUBLISHED"):
            return status

        log.debug(f"Container {container_id} status: {status}, waiting...")
        time.sleep(2)

    raise TimeoutError(f"Container {container_id} didn't finish in {max_wait}s")


def publish_single(image_url: str, caption: str) -> dict:
    """Publish a single image to Instagram.

    Two-step process:
    1. Create media container with image_url and caption
    2. Publish the container

    Returns {"id": media_id} on success.
    """
    token, user_id = get_ig_credentials()

    # Step 1: Create media container
    log.info(f"Creating media container for {image_url[:80]}...")
    resp = requests.post(
        f"{GRAPH_API_BASE}/{user_id}/media",
        data={
            "image_url": image_url,
            "caption": caption,
            "access_token": token,
        },
        timeout=30,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"Create container failed: {resp.status_code} {resp.text}")

    container_id = resp.json()["id"]
    log.info(f"Container created: {container_id}")

    # Step 2: Wait for container to be ready
    _check_container_status(container_id, token)

    # Step 3: Publish
    log.info("Publishing...")
    resp = requests.post(
        f"{GRAPH_API_BASE}/{user_id}/media_publish",
        data={
            "creation_id": container_id,
            "access_token": token,
        },
        timeout=30,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"Publish failed: {resp.status_code} {resp.text}")

    result = resp.json()
    log.info(f"Published! Media ID: {result['id']}")
    return result


def publish_carousel(image_urls: list[str], caption: str) -> dict:
    """Publish a carousel (multi-image) post to Instagram.

    Three-step process:
    1. Create child containers for each image (no caption)
    2. Create carousel container referencing children + caption
    3. Publish the carousel container

    Returns {"id": media_id} on success.
    """
    token, user_id = get_ig_credentials()

    if len(image_urls) < 2:
        raise ValueError("Carousel requires at least 2 images")
    if len(image_urls) > 10:
        raise ValueError("Carousel supports max 10 images")

    # Step 1: Create child containers
    child_ids = []
    for i, url in enumerate(image_urls):
        log.info(f"Creating child container {i+1}/{len(image_urls)}...")
        resp = requests.post(
            f"{GRAPH_API_BASE}/{user_id}/media",
            data={
                "image_url": url,
                "is_carousel_item": "true",
                "access_token": token,
            },
            timeout=30,
        )
        if resp.status_code != 200:
            raise RuntimeError(f"Child container {i+1} failed: {resp.status_code} {resp.text}")
        child_id = resp.json()["id"]
        child_ids.append(child_id)
        log.info(f"Child container {i+1}: {child_id}")

    # Wait for all children to finish processing
    for child_id in child_ids:
        _check_container_status(child_id, token)

    # Step 2: Create carousel container
    log.info("Creating carousel container...")
    resp = requests.post(
        f"{GRAPH_API_BASE}/{user_id}/media",
        data={
            "media_type": "CAROUSEL",
            "children": ",".join(child_ids),
            "caption": caption,
            "access_token": token,
        },
        timeout=30,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"Carousel container failed: {resp.status_code} {resp.text}")

    carousel_id = resp.json()["id"]
    log.info(f"Carousel container: {carousel_id}")

    # Wait for carousel to be ready
    _check_container_status(carousel_id, token)

    # Step 3: Publish
    log.info("Publishing carousel...")
    resp = requests.post(
        f"{GRAPH_API_BASE}/{user_id}/media_publish",
        data={
            "creation_id": carousel_id,
            "access_token": token,
        },
        timeout=30,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"Carousel publish failed: {resp.status_code} {resp.text}")

    result = resp.json()
    log.info(f"Carousel published! Media ID: {result['id']}")
    return result
