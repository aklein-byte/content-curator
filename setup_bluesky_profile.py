#!/usr/bin/env python3
"""
One-time Bluesky profile setup utility.
Sets display name, bio, avatar, and banner for each account.

Usage:
    python setup_bluesky_profile.py --niche tatamispaces
    python setup_bluesky_profile.py --niche museumstories
    python setup_bluesky_profile.py --niche cosmicshots
    python setup_bluesky_profile.py --all
"""

import sys
import argparse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from dotenv import load_dotenv
load_dotenv()

from tools.bluesky import set_niche, _get_client
from config.niches import get_niche, list_niches


def setup_profile(niche_id: str):
    """Set up Bluesky profile for a niche."""
    niche = get_niche(niche_id)

    if not niche.get("bluesky_env"):
        print(f"  Skipping {niche_id} — no bluesky_env configured")
        return

    profile_cfg = niche.get("bluesky_profile", {})
    if not profile_cfg:
        print(f"  Skipping {niche_id} — no bluesky_profile configured")
        return

    set_niche(niche_id)
    client = _get_client()

    display_name = profile_cfg.get("display_name", "")
    description = profile_cfg.get("description", "")

    # Upload avatar if path exists
    avatar_blob = None
    avatar_path = profile_cfg.get("avatar_path")
    if avatar_path:
        p = Path(__file__).parent / avatar_path
        if p.exists():
            data = p.read_bytes()
            resp = client.upload_blob(data)
            avatar_blob = resp.blob
            print(f"  Uploaded avatar: {p.name}")
        else:
            print(f"  Avatar not found: {p}")

    # Upload banner if path exists
    banner_blob = None
    banner_path = profile_cfg.get("banner_path")
    if banner_path:
        p = Path(__file__).parent / banner_path
        if p.exists():
            data = p.read_bytes()
            resp = client.upload_blob(data)
            banner_blob = resp.blob
            print(f"  Uploaded banner: {p.name}")
        else:
            print(f"  Banner not found: {p}")

    # Get current profile to preserve fields we're not setting
    current = client.app.bsky.actor.get_profile({"actor": client.me.did})

    # Build profile record
    record = {
        "$type": "app.bsky.actor.profile",
        "displayName": display_name or current.display_name or "",
        "description": description or current.description or "",
    }
    if avatar_blob:
        record["avatar"] = avatar_blob
    elif current.avatar:
        # Keep existing avatar
        pass
    if banner_blob:
        record["banner"] = banner_blob
    elif current.banner:
        # Keep existing banner
        pass

    # Write profile via repo API
    try:
        # Try to get existing record first
        existing = client.app.bsky.actor.profile.get(client.me.did, "self")
        # Swap (update) the record
        client.com.atproto.repo.put_record(
            data={
                "repo": client.me.did,
                "collection": "app.bsky.actor.profile",
                "rkey": "self",
                "record": record,
                "swapRecord": existing.value.model_dump().get("cid"),
            }
        )
    except Exception:
        # Create new profile record
        client.com.atproto.repo.put_record(
            data={
                "repo": client.me.did,
                "collection": "app.bsky.actor.profile",
                "rkey": "self",
                "record": record,
            }
        )

    print(f"  Profile updated: {display_name}")
    print(f"  Bio: {description[:80]}...")


def main():
    parser = argparse.ArgumentParser(description="Set up Bluesky profiles")
    parser.add_argument("--niche", help="Niche ID to set up")
    parser.add_argument("--all", action="store_true", help="Set up all niches with bluesky_env")
    args = parser.parse_args()

    if args.all:
        for niche_id in list_niches():
            niche = get_niche(niche_id)
            if niche.get("bluesky_env"):
                print(f"\nSetting up {niche_id}...")
                try:
                    setup_profile(niche_id)
                except Exception as e:
                    print(f"  ERROR: {e}")
    elif args.niche:
        print(f"Setting up {args.niche}...")
        setup_profile(args.niche)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
