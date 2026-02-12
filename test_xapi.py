#!/usr/bin/env python3
"""Quick test of X API v2 endpoints."""
from dotenv import load_dotenv
load_dotenv()
import os, requests, json
from requests_oauthlib import OAuth1

auth = OAuth1(
    os.environ["X_API_CONSUMER_KEY"],
    os.environ["X_API_CONSUMER_SECRET"],
    os.environ["X_API_ACCESS_TOKEN"],
    os.environ["X_API_ACCESS_TOKEN_SECRET"],
)
USER_ID = "2017827047129718784"

# 1. Search
print("=== SEARCH ===")
r = requests.get("https://api.twitter.com/2/tweets/search/recent",
    params={
        "query": "japanese architecture has:images -is:retweet",
        "max_results": 10,
        "tweet.fields": "public_metrics,author_id,created_at",
        "expansions": "author_id,attachments.media_keys",
        "media.fields": "url,type,preview_image_url",
        "user.fields": "username,name",
    },
    auth=auth, timeout=10)
print("Status:", r.status_code)
data = r.json()
if "data" in data:
    users = {u["id"]: u for u in data.get("includes", {}).get("users", [])}
    media = {m["media_key"]: m for m in data.get("includes", {}).get("media", [])}
    print(f"Got {len(data['data'])} tweets")
    test_tweet_id = None
    for t in data["data"][:3]:
        u = users.get(t["author_id"], {})
        m = t.get("public_metrics", {})
        likes = m.get("like_count", 0)
        handle = u.get("username", "?")
        print(f"  @{handle} - {likes} likes: {t['text'][:60]}")
        if not test_tweet_id:
            test_tweet_id = t["id"]
    print(f"Rate limit: {r.headers.get('x-rate-limit-remaining')}/{r.headers.get('x-rate-limit-limit')}")
else:
    print(json.dumps(data, indent=2)[:500])

# 2. Like (dry — just check if endpoint is accessible)
print("\n=== LIKE (test) ===")
# Don't actually like, just check we can hit the endpoint format
print(f"Would POST to /2/users/{USER_ID}/likes with tweet_id={test_tweet_id}")

# 3. Check what tier we're on via usage
print("\n=== USAGE ===")
r3 = requests.get("https://api.twitter.com/2/usage/tweets",
    params={"days": 7},
    auth=auth, timeout=10)
print("Usage endpoint:", r3.status_code)
if r3.status_code == 200:
    print(json.dumps(r3.json(), indent=2)[:300])
else:
    print(r3.text[:200])
