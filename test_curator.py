#!/usr/bin/env python3
"""
Test script for the curator agent.
Run with: python test_curator.py

Tests:
1. Simple conversation with curator (no scraping)
2. Caption writing
"""

import asyncio
import os
from dotenv import load_dotenv

load_dotenv()

# Verify API key is set
if not os.environ.get("ANTHROPIC_API_KEY"):
    print("Error: ANTHROPIC_API_KEY not set")
    print("Copy .env.example to .env and add your key")
    exit(1)

from agents.curator import curate_with_conversation, CURATOR_MODEL
from agents.writer import write_caption, WRITER_MODEL
from tools.storage import init_db


async def test_curator_conversation():
    """Test curator agent with a simple question."""
    print("\n" + "="*50)
    print("TEST 1: Curator Conversation")
    print(f"Model: {CURATOR_MODEL}")
    print("="*50 + "\n")

    response = await curate_with_conversation(
        niche_id="tatamispaces",
        user_request="""I'm looking for images of wabi-sabi interiors.

What specific visual elements should I look for that indicate authentic wabi-sabi
versus staged "minimalist" aesthetics that just try to look Japanese?

Give me 5 specific things to look for."""
    )

    print(response)
    return True


async def test_writer():
    """Test caption writing."""
    print("\n" + "="*50)
    print("TEST 2: Caption Writer")
    print(f"Model: {WRITER_MODEL}")
    print("="*50 + "\n")

    result = await write_caption(
        niche_id="tatamispaces",
        image_context="A traditional Japanese room with aged tatami mats, morning light filtering through shoji screens, a simple tokonoma alcove with a single ceramic vase",
        source_name="Suppose Design Office",
        curator_notes="Beautiful example of lived-in wabi-sabi. The wear on the tatami shows this is a real space, not a showroom. The light is remarkable.",
    )

    print(f"Caption: {result['caption']}")
    print(f"Hashtags: {' '.join(result['hashtags'])}")
    return True


async def main():
    """Run all tests."""
    print("Content Curator Test Suite")
    print("="*50)

    # Initialize database
    await init_db()
    print("Database initialized")

    # Run tests
    tests = [
        ("Curator Conversation", test_curator_conversation),
        ("Caption Writer", test_writer),
    ]

    results = []
    for name, test_fn in tests:
        try:
            success = await test_fn()
            results.append((name, success, None))
        except Exception as e:
            results.append((name, False, str(e)))

    # Summary
    print("\n" + "="*50)
    print("TEST SUMMARY")
    print("="*50)

    for name, success, error in results:
        status = "✅ PASS" if success else "❌ FAIL"
        print(f"{status}: {name}")
        if error:
            print(f"   Error: {error}")

    return all(r[1] for r in results)


if __name__ == "__main__":
    success = asyncio.run(main())
    exit(0 if success else 1)
