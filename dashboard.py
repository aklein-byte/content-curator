"""
Post management dashboard for @tatamispaces and Museum Stories.
Single page with niche toggle.

Usage: python dashboard.py [--port 8080]
Opens at http://localhost:8080
  /?niche=tatamispaces   — tatami view (default)
  /?niche=museumstories  — museum view
"""

import json
import os
import sys
import asyncio
import argparse
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse
from pathlib import Path
from datetime import datetime, timezone

BASE_DIR = Path(__file__).parent
TEMPLATES_DIR = BASE_DIR / "templates"

# Load .env so writer agent can find ANTHROPIC_API_KEY
from dotenv import load_dotenv
load_dotenv(BASE_DIR / ".env")

sys.path.insert(0, str(BASE_DIR))
from config.niches import get_niche, list_niches
from tools.post_queue import (
    load_posts, update_post, get_post,
)
from tools.db import get_db, json_dumps

# URL path -> niche_id shortcut routes (e.g., /museum -> museumstories)
_NICHE_ROUTES = {n: n for n in list_niches()}
_NICHE_ROUTES["museum"] = "museumstories"
_NICHE_ROUTES["tatami"] = "tatamispaces"


class DashboardHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # quiet

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/" or parsed.path == "":
            self.serve_dashboard()
        elif parsed.path.strip("/") in _NICHE_ROUTES:
            # Serve niche view directly (avoids redirect issues behind nginx proxy)
            self.path = f"/?niche={_NICHE_ROUTES[parsed.path.strip('/')]}"
            self.serve_dashboard()
        elif parsed.path == "/api/export":
            self.handle_export()
        else:
            self.send_error(404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length))

        if self.path == "/api/status":
            post_id = body.get("id")
            new_status = body.get("status")
            niche_id = body.get("niche", "tatamispaces")

            if not post_id or not new_status:
                self.send_json({"ok": False, "error": "missing id or status"})
                return

            post = get_post(niche_id, post_id)
            if not post:
                self.send_json({"ok": False, "error": "post not found"})
                return

            fields = {"status": new_status}
            if new_status == "approved":
                fields["ig_skip_reason"] = None
                fields["skip_reason"] = None
                if not post.get("scheduled_for"):
                    fields["scheduled_for"] = datetime.now(timezone.utc).isoformat()
            update_post(niche_id, post_id, **fields)
            self.send_json({"ok": True})

        elif self.path == "/api/text-edit":
            self.handle_text_edit(body)

        elif self.path == "/api/image-select":
            post_id = body.get("id")
            niche_id = body.get("niche", "tatamispaces")
            if not post_id:
                self.send_json({"ok": False, "error": "missing id"})
                return

            post = get_post(niche_id, post_id)
            if not post:
                self.send_json({"ok": False, "error": "post not found"})
                return

            fields = {}
            if body.get("image_index") is not None:
                fields["image_index"] = body["image_index"]
                fields["image_count"] = None
            else:
                fields["image_index"] = None

            if body.get("image_count") is not None:
                fields["image_count"] = body["image_count"]
            elif "image_index" not in body:
                fields["image_count"] = None

            update_post(niche_id, post_id, **fields)
            self.send_json({"ok": True})

        elif self.path == "/api/museum/status":
            self.handle_museum_status(body)

        elif self.path == "/api/museum/tweet-edit":
            self.handle_museum_tweet_edit(body)

        elif self.path == "/api/museum/image-assign":
            self.handle_museum_image_assign(body)

        elif self.path == "/api/museum/notes":
            self.handle_museum_notes(body)

        elif self.path == "/api/regenerate":
            self.handle_regenerate(body)

        elif self.path == "/api/museum/regenerate":
            self.handle_museum_regenerate(body)

        else:
            self.send_error(404)

    # === Post API handlers ===

    def handle_text_edit(self, body):
        """Update a simple post's text field."""
        post_id = body.get("id")
        text = body.get("text")
        niche_id = body.get("niche", "tatamispaces")
        if not post_id or text is None:
            self.send_json({"ok": False, "error": "missing id or text"})
            return
        update_post(niche_id, post_id, text=text)
        self.send_json({"ok": True})

    def handle_museum_status(self, body):
        post_id = body.get("id")
        new_status = body.get("status")
        niche_id = body.get("niche", "museumstories")
        if not post_id or not new_status:
            self.send_json({"ok": False, "error": "missing id or status"})
            return

        post = get_post(niche_id, post_id)
        if not post:
            self.send_json({"ok": False, "error": "post not found"})
            return

        fields = {"status": new_status}
        if new_status == "approved" and not post.get("scheduled_for"):
            fields["scheduled_for"] = datetime.now(timezone.utc).isoformat()
        update_post(niche_id, post_id, **fields)
        self.send_json({"ok": True})

    def handle_museum_tweet_edit(self, body):
        post_id = body.get("id")
        tweet_index = body.get("tweet_index")
        text = body.get("text")
        niche_id = body.get("niche", "museumstories")
        if post_id is None or tweet_index is None or text is None:
            self.send_json({"ok": False, "error": "missing id, tweet_index, or text"})
            return

        db = get_db()
        result = db.execute(
            "UPDATE museum_tweets SET text = ? WHERE niche_id = ? AND post_id = ? AND tweet_index = ?",
            (text, niche_id, post_id, tweet_index),
        )
        db.commit()
        if result.rowcount > 0:
            self.send_json({"ok": True})
        else:
            self.send_json({"ok": False, "error": "tweet not found"})

    def handle_museum_image_assign(self, body):
        post_id = body.get("post_id")
        tweet_index = body.get("tweet_index")
        image_index = body.get("image_index")
        action = body.get("action")
        niche_id = body.get("niche", "museumstories")
        if post_id is None or tweet_index is None or image_index is None or action not in ("add", "remove"):
            self.send_json({"ok": False, "error": "missing post_id, tweet_index, image_index, or action"})
            return

        db = get_db()
        row = db.execute(
            "SELECT images FROM museum_tweets WHERE niche_id = ? AND post_id = ? AND tweet_index = ?",
            (niche_id, post_id, tweet_index),
        ).fetchone()

        if not row:
            self.send_json({"ok": False, "error": "tweet not found"})
            return

        from tools.db import json_loads
        images = json_loads(row["images"], default=[])

        if action == "add":
            if image_index not in images:
                images.append(image_index)
        elif action == "remove":
            images = [i for i in images if i != image_index]

        db.execute(
            "UPDATE museum_tweets SET images = ? WHERE niche_id = ? AND post_id = ? AND tweet_index = ?",
            (json_dumps(images), niche_id, post_id, tweet_index),
        )
        db.commit()
        self.send_json({"ok": True})

    def handle_museum_notes(self, body):
        post_id = body.get("id")
        niche_id = body.get("niche", "museumstories")
        if post_id is None:
            self.send_json({"ok": False, "error": "missing id"})
            return

        fields = {}
        if "vote" in body:
            fields["vote"] = body["vote"]
        if "notes" in body:
            fields["notes"] = body["notes"]

        if fields:
            update_post(niche_id, post_id, **fields)
        self.send_json({"ok": True})

    # === Regenerate handlers ===

    def handle_regenerate(self, body):
        """Regenerate a tatami post caption via writer agent."""
        post_id = body.get("id")
        niche_id = body.get("niche", "tatamispaces")
        feedback = body.get("feedback", "Try a different angle. Keep the same facts but find a fresher way to say it.")
        if not post_id:
            self.send_json({"ok": False, "error": "missing id"})
            return

        post = get_post(niche_id, post_id)
        if not post:
            self.send_json({"ok": False, "error": "post not found"})
            return

        original = post.get("text", "")
        if not original:
            self.send_json({"ok": False, "error": "post has no text"})
            return

        try:
            from agents.writer import rewrite_caption
            result = asyncio.run(rewrite_caption(niche_id, original, feedback))
            update_post(niche_id, post_id, text=result["caption"], _previous_text=original)
            self.send_json({"ok": True, "caption": result["caption"]})
        except Exception as e:
            self.send_json({"ok": False, "error": str(e)})

    def handle_museum_regenerate(self, body):
        """Regenerate a tweet caption via writer agent."""
        post_id = body.get("id")
        tweet_index = body.get("tweet_index")
        niche_id = body.get("niche", "museumstories")
        feedback = body.get("feedback", "Try a different angle. Keep the same facts but find a fresher way to say it.")
        if post_id is None or tweet_index is None:
            self.send_json({"ok": False, "error": "missing id or tweet_index"})
            return

        post = get_post(niche_id, post_id)
        if not post:
            self.send_json({"ok": False, "error": "post not found"})
            return

        tweets = post.get("tweets", [])
        if not (0 <= tweet_index < len(tweets)):
            self.send_json({"ok": False, "error": "invalid tweet_index"})
            return

        original = tweets[tweet_index]["text"]
        try:
            from agents.writer import rewrite_caption
            result = asyncio.run(rewrite_caption(niche_id, original, feedback))

            db = get_db()
            db.execute(
                "UPDATE museum_tweets SET text = ?, _previous_text = ? WHERE niche_id = ? AND post_id = ? AND tweet_index = ?",
                (result["caption"], original, niche_id, post_id, tweet_index),
            )
            db.commit()
            self.send_json({"ok": True, "caption": result["caption"]})
        except Exception as e:
            self.send_json({"ok": False, "error": str(e)})

    # === Export endpoint ===

    def handle_export(self):
        """Export posts as JSON for debugging/backup."""
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        niche_id = qs.get("niche", ["tatamispaces"])[0]

        data = load_posts(niche_id)
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Disposition", f'attachment; filename="posts-{niche_id}.json"')
        self.end_headers()
        self.wfile.write(json.dumps(data, indent=2, ensure_ascii=False, default=str).encode())

    # === Dashboard serve ===

    def serve_dashboard(self):
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        niche = qs.get("niche", ["tatamispaces"])[0]

        html = (TEMPLATES_DIR / "dashboard.html").read_text()
        html = html.replace("__NICHE__", niche)

        # All niches: client-rendered from JSON
        niche_data = load_posts(niche)
        posts_json = json.dumps(niche_data.get("posts", []), ensure_ascii=False, default=str)
        html = html.replace("__POSTS_DATA__", posts_json)

        # Set active nav tab
        for nid in list_niches():
            placeholder = f"__ACTIVE_{nid.upper()}__"
            html = html.replace(placeholder, "active" if nid == niche else "")

        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(html.encode())

    def send_json(self, data):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())


def main():
    parser = argparse.ArgumentParser(description="Post management dashboard")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()

    server = HTTPServer(("127.0.0.1", args.port), DashboardHandler)
    print(f"Dashboard running at http://localhost:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped")


if __name__ == "__main__":
    main()
