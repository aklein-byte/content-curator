"""
Post management dashboard for @tatamispaces and Museum Stories.
Simple web UI to preview, approve, skip posts, and pick images.

Usage: python dashboard.py [--port 8080]
Opens at http://localhost:8080
  /          — tatami dashboard
  /museum/   — museum stories dashboard
"""

import json
import os
import sys
import argparse
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse
from pathlib import Path
from datetime import datetime, timezone

BASE_DIR = Path(__file__).parent
POSTS_FILE = BASE_DIR / "posts.json"


def load_posts():
    data = json.loads(POSTS_FILE.read_text())
    if isinstance(data, list):
        data = {"posts": data}
    return data


def save_posts(data):
    POSTS_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False))


TEMPLATES_DIR = BASE_DIR / "templates"
MUSEUM_POSTS_FILE = BASE_DIR / "posts-museum-curated.json"


def load_museum_posts():
    if not MUSEUM_POSTS_FILE.exists():
        return {"posts": []}
    data = json.loads(MUSEUM_POSTS_FILE.read_text())
    if isinstance(data, list):
        data = {"posts": data}
    return data


def save_museum_posts(data):
    MUSEUM_POSTS_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False))


def render_post_html(post, index):
    pid = post.get("id", index)
    status = post.get("status", "unknown")
    text = post.get("text", "")
    source = post.get("source", "")
    source_handle = post.get("source_handle", "")
    category = post.get("category", "")
    tweet_id = post.get("tweet_id", "")
    ig_posted = post.get("ig_posted", False)
    posted_at = post.get("posted_at", "")
    scheduled = post.get("scheduled_for", "")

    # Image URLs — build both thumbnail and full-res lists
    image_urls = post.get("image_urls") or []
    img_index = post.get("image_index")
    img_count = post.get("image_count")

    # Full-res URLs for lightbox (orig quality)
    full_urls = []
    for url in image_urls[:4]:
        full = url
        if "pbs.twimg.com" in url and "format=" not in url:
            full = url + "?format=jpg&name=orig"
        elif "format=" in url:
            full = url.replace("name=small", "name=orig")
        full_urls.append(full)

    # JSON-safe URL list for onclick
    import html as html_mod
    urls_json = html_mod.escape(json.dumps(full_urls))

    imgs_html = ""
    for idx, url in enumerate(image_urls[:4]):
        # Thumbnail URL
        thumb = url
        if "pbs.twimg.com" in url and "format=" not in url:
            thumb = url + "?format=jpg&name=small"
        elif "format=" in url:
            thumb = url.replace("name=orig", "name=small")

        selected = ""
        if img_index is not None and idx == img_index:
            selected = " selected"
        elif img_index is not None and idx != img_index:
            selected = " dimmed"
        elif img_count is not None and idx >= img_count:
            selected = " dimmed"

        imgs_html += f'<div class="img-wrap{selected}"><span class="img-num">{idx+1}</span><img src="{thumb}" loading="lazy" onerror="this.parentElement.style.display=\'none\'" onclick="openLightbox({pid}, {urls_json}, {idx})"></div>'

    if len(image_urls) > 1 and status in ("approved", "dropped") or (status.startswith("skipped") and len(image_urls) > 1):
        current = "all"
        if img_index is not None:
            current = f"#{img_index+1} only"
        elif img_count is not None:
            current = f"first {img_count}"
        imgs_html += f'<div class="img-controls">Using: {current} <button onclick="resetImages({pid})">all</button></div>'

    # Badges
    badges = ""
    badge_class = f"badge-{status}" if status in ("posted", "approved", "dropped", "draft") else "badge-skipped"
    if status.startswith("skipped"):
        badges += f'<span class="badge badge-skipped">skipped</span>'
    else:
        badges += f'<span class="badge {badge_class}">{status}</span>'

    post_type = post.get("type", "")
    if post_type == "quote-tweet":
        badges += '<span class="badge" style="background:#1d9bf0;color:#fff">QT</span>'
    if tweet_id:
        badges += '<span class="badge badge-x">X</span>'
    if ig_posted:
        badges += '<span class="badge badge-ig">IG</span>'

    # Meta info
    meta = ""
    if source_handle:
        meta += f'<span>from: {source_handle}</span>'
    elif source:
        meta += f'<span>source: {source}</span>'
    quote_id = post.get("quote_tweet_id")
    if quote_id:
        qt_handle = (source_handle or "").lstrip("@") or "i"
        meta += f'<span>quoting: <a href="https://x.com/{qt_handle}/status/{quote_id}" target="_blank" style="color:#1d9bf0">view original</a></span>'
    if category:
        meta += f'<span>category: {category}</span>'
    if posted_at:
        meta += f'<span>posted: {posted_at[:16]}</span>'
    elif scheduled:
        meta += f'<span>scheduled: {scheduled[:16]}</span>'

    score = post.get("score")
    if score is not None:
        meta += f'<span>score: {score}/10</span>'

    # Actions
    actions = ""
    if status == "draft":
        actions = f'<button class="btn-approve" onclick="setStatus({pid},\'approved\')">Approve</button> <button class="btn-skip" onclick="setStatus({pid},\'dropped\')">Delete</button>'
    elif status == "approved":
        actions = f'<button class="btn-skip" onclick="setStatus({pid},\'dropped\')">Skip</button>'
    elif status == "dropped" or status.startswith("skipped"):
        actions = f'<button class="btn-approve" onclick="setStatus({pid},\'approved\')">Approve</button>'

    css_class = "posted" if status == "posted" else ("dropped" if status == "dropped" or status.startswith("skipped") else "")

    return f'''<div class="post {css_class}" data-status="{status}" data-id="{pid}">
      <div class="post-images">{imgs_html}</div>
      <div class="post-body">
        <div class="post-id">#{pid} {badges}</div>
        <div class="post-text">{text}</div>
        <div class="post-meta">{meta}</div>
        <div class="post-actions">{actions}</div>
      </div>
    </div>'''


class DashboardHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # quiet

    def do_GET(self):
        if self.path == "/" or self.path.startswith("/?"):
            self.serve_dashboard()
        elif self.path == "/museum/" or self.path == "/museum" or self.path.startswith("/museum/?"):
            self.serve_museum_dashboard()
        else:
            self.send_error(404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length))

        if self.path == "/api/status":
            post_id = body.get("id")
            new_status = body.get("status")

            if not post_id or not new_status:
                self.send_json({"ok": False, "error": "missing id or status"})
                return

            data = load_posts()
            found = False
            for p in data.get("posts", []):
                if p.get("id") == post_id:
                    p["status"] = new_status
                    if new_status == "approved":
                        p.pop("ig_skip_reason", None)
                        p.pop("skip_reason", None)
                    found = True
                    break

            if found:
                save_posts(data)
                self.send_json({"ok": True})
            else:
                self.send_json({"ok": False, "error": "post not found"})

        elif self.path == "/api/image-select":
            post_id = body.get("id")
            if not post_id:
                self.send_json({"ok": False, "error": "missing id"})
                return

            data = load_posts()
            found = False
            for p in data.get("posts", []):
                if p.get("id") == post_id:
                    if body.get("image_index") is not None:
                        p["image_index"] = body["image_index"]
                        p.pop("image_count", None)
                    else:
                        p.pop("image_index", None)

                    if body.get("image_count") is not None:
                        p["image_count"] = body["image_count"]
                    elif "image_index" not in body:
                        p.pop("image_count", None)

                    found = True
                    break

            if found:
                save_posts(data)
                self.send_json({"ok": True})
            else:
                self.send_json({"ok": False, "error": "post not found"})

        elif self.path == "/museum/api/status":
            self.handle_museum_status(body)

        elif self.path == "/museum/api/tweet-edit":
            self.handle_museum_tweet_edit(body)

        elif self.path == "/museum/api/image-assign":
            self.handle_museum_image_assign(body)

        elif self.path == "/museum/api/notes":
            self.handle_museum_notes(body)

        else:
            self.send_error(404)

    # === Museum API handlers ===

    def handle_museum_status(self, body):
        post_id = body.get("id")
        new_status = body.get("status")
        if not post_id or not new_status:
            self.send_json({"ok": False, "error": "missing id or status"})
            return
        data = load_museum_posts()
        for p in data["posts"]:
            if p["id"] == post_id:
                p["status"] = new_status
                save_museum_posts(data)
                self.send_json({"ok": True})
                return
        self.send_json({"ok": False, "error": "post not found"})

    def handle_museum_tweet_edit(self, body):
        post_id = body.get("id")
        tweet_index = body.get("tweet_index")
        text = body.get("text")
        if post_id is None or tweet_index is None or text is None:
            self.send_json({"ok": False, "error": "missing id, tweet_index, or text"})
            return
        data = load_museum_posts()
        for p in data["posts"]:
            if p["id"] == post_id:
                if 0 <= tweet_index < len(p.get("tweets", [])):
                    p["tweets"][tweet_index]["text"] = text
                    save_museum_posts(data)
                    self.send_json({"ok": True})
                    return
                self.send_json({"ok": False, "error": "invalid tweet_index"})
                return
        self.send_json({"ok": False, "error": "post not found"})

    def handle_museum_image_assign(self, body):
        post_id = body.get("post_id")
        tweet_index = body.get("tweet_index")
        image_index = body.get("image_index")
        action = body.get("action")
        if post_id is None or tweet_index is None or image_index is None or action not in ("add", "remove"):
            self.send_json({"ok": False, "error": "missing post_id, tweet_index, image_index, or action"})
            return
        data = load_museum_posts()
        for p in data["posts"]:
            if p["id"] == post_id:
                if 0 <= tweet_index < len(p.get("tweets", [])):
                    tweet = p["tweets"][tweet_index]
                    if "images" not in tweet:
                        tweet["images"] = []
                    if action == "add":
                        if image_index not in tweet["images"]:
                            tweet["images"].append(image_index)
                    elif action == "remove":
                        tweet["images"] = [i for i in tweet["images"] if i != image_index]
                    save_museum_posts(data)
                    self.send_json({"ok": True})
                    return
                self.send_json({"ok": False, "error": "invalid tweet_index"})
                return
        self.send_json({"ok": False, "error": "post not found"})

    def handle_museum_notes(self, body):
        post_id = body.get("id")
        if post_id is None:
            self.send_json({"ok": False, "error": "missing id"})
            return
        data = load_museum_posts()
        for p in data["posts"]:
            if p["id"] == post_id:
                if "vote" in body:
                    p["vote"] = body["vote"]
                if "notes" in body:
                    p["notes"] = body["notes"]
                save_museum_posts(data)
                self.send_json({"ok": True})
                return
        self.send_json({"ok": False, "error": "post not found"})

    # === Dashboard serve methods ===

    def serve_dashboard(self):
        data = load_posts()
        posts = data.get("posts", [])

        counts = {"all": len(posts), "approved": 0, "posted": 0, "dropped": 0}
        for p in posts:
            s = p.get("status", "")
            if s == "approved":
                counts["approved"] += 1
            elif s == "posted":
                counts["posted"] += 1
            elif s == "dropped" or s.startswith("skipped"):
                counts["dropped"] += 1

        posts_html = ""
        for i, p in enumerate(posts):
            posts_html += render_post_html(p, i)

        html = (TEMPLATES_DIR / "tatami.html").read_text()
        html = html.replace("POSTS_HTML", posts_html)
        html = html.replace("POST_COUNT_ALL", str(counts["all"]))
        html = html.replace("POST_COUNT_APPROVED", str(counts["approved"]))
        html = html.replace("POST_COUNT_POSTED", str(counts["posted"]))
        html = html.replace("POST_COUNT_DROPPED", str(counts["dropped"]))

        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(html.encode())

    def serve_museum_dashboard(self):
        data = load_museum_posts()
        posts_json = json.dumps(data.get("posts", []), ensure_ascii=False)
        html = (TEMPLATES_DIR / "museum.html").read_text()
        html = html.replace("__POSTS_DATA__", posts_json)
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

    server = HTTPServer(("0.0.0.0", args.port), DashboardHandler)
    print(f"Dashboard running at http://localhost:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped")


if __name__ == "__main__":
    main()
